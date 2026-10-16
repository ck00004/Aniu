# 生产启动阻塞诊断与修复报告

- 日期：2026-10-16
- 现象：生产环境 `uvicorn app.main:app --reload` 卡在 `Waiting for application startup.` 无后续输出
- 验证：后端 pytest **322 通过**（318 + 4 新增启动专项测试）；真实 uvicorn 启动日志验证（每步耗时可见，总启动 0.06s）

---

## 一、诊断结论

启动关键路径上定位到三个风险点（按可能性排序）：

### 1. 历史数据回填每次启动全量重跑（最可能的主因）
`init_db()` 里的 `_backfill_strategy_run_types` **每次启动**都：
- 全表 SELECT 所有 run 的**完整 JSON 载荷**（executed_actions/skill_payloads/decision_payload，生产每条可达数百 KB——含全部工具调用转录）；
- 对**每一条** run 执行 UPDATE（即使值没有任何变化）；
- 单个大事务最后 commit。

本地用 3000 条 run × 200KB（1.8GB 库）实测 SSD 上 1.1 秒——但生产 VPS 的慢速云盘/网络盘会放大 10-100 倍，且库随运行天数线性增长（30 分钟调度 ≈ 48 条/天）。数月数据 + 慢盘 = 启动数分钟无响应，表现就是"卡在 Waiting for application startup"。

### 2. 日历预热的网络最坏情况
预热在启动关键路径上同步执行：远程日历接口（ciis.com.hk）网络黑洞时最坏 4 次尝试 × 30 秒超时 + 退避 ≈ **126 秒**。随包的日历缓存文件覆盖 2026-01~2026-12（git 已跟踪），正常部署当月命中缓存不会触发网络；但缓存文件缺失/过期时此路径会显著拖慢启动。此外启动期的僵尸 run 收敛（推进调度 next_run_at）也可能触发同一路径。

### 3. 应用日志不可见（排障障碍，非阻塞）
uvicorn 默认只配置自己的 logger——应用 logger 的 INFO 输出**没有任何 handler**，即使卡住也不会有任何应用侧日志帮助定位。这解释了为什么用户只看到 uvicorn 的三行输出。

## 二、修复内容

### 修复 1：启动全路径分步计时日志（直接回答"能否打日志排查"）
- `main.py` lifespan：每个步骤完成即输出 `app startup: <步骤> done in X.XXs`（init_db / 技能重载 / 状态恢复 / 日历预热调度 / 调度器启动），最后输出 `all steps finished in X.XXs, serving requests`。
- `database.py` init_db：每个迁移步骤输出 `init_db step: <步骤> done in X.XXs`；回填每 500 条输出进度（已处理/总数/变更数）。
- **`_configure_app_logging()`**：根 logger 无 handler 时（uvicorn 场景）为 `app`/`skills` logger 挂载控制台 handler——否则以上日志全部不可见。pytest（caplog 自带 handler）不干预。
- 日历拉取每次失败输出 WARNING（第几次/共几次/月份/原因）。

真实启动日志示例（修复后）：
```
INFO:     app.db.database : init_db step: create_all done in 0.02s
INFO:     app.db.database : backfill_strategy_run_types: one-time migration starting
INFO:     app.db.database : backfill_strategy_run_types: finished 0 rows, 0 changed, 0.00s
INFO:     app.main : app startup: init_db done in 0.04s
INFO:     app.main : app startup: skill_registry.reload done in 0.01s
...
INFO:     app.main : app startup: all steps finished in 0.06s, serving requests
INFO:     Application startup complete.
```

### 修复 2：回填一次性化（同时解决审计 P2-30）
- 新增 `schema_meta` 键值表（`CREATE TABLE IF NOT EXISTS`）：回填完成后落标记 `backfill_strategy_run_types_v1=done`，**后续启动直接跳过**（日志可见 `skipped (marker set)`）。
- 首次执行也做了写放大优化：**仅 UPDATE 值实际变化的行**（绝大多数行不变 → 几乎零写入）。
- 保留原有语义：清掉标记可强制重跑（测试中用于验证）。

### 修复 3：日历预热移出启动关键路径
- 预热改为**后台守护线程**（`aniu-calendar-warmup`）：即使网络黑洞 126 秒，启动也不再等待——完成/失败都有日志。
- 拉取参数收紧：超时 30s → **10s**、重试 3 → **2 次**（最坏 ≈ 34 秒，正常时秒级返回）；运行期懒加载 + 5 分钟失败负缓存不变。

## 三、测试（4 个新增回归测试）

- **慢日历不阻塞启动**：mock 预热睡 3 秒，TestClient 启动 < 2 秒且预热确实被调度
- **分步日志齐全**：caplog 验证 5 个关键步骤日志全部输出
- **回填一次性标记**：清标记 → init_db 归类旧数据（上午运行→trade）→ 再破坏数据 → 第二次 init_db 不再改写（标记生效）
- **回填值保持**：正确值的行回填后不变 + 标记落库
- 适配 2 个既有日历重试测试（重试次数 3→2）

完整套件 **322 通过 / 0 失败**。

## 四、生产排查指引（部署本版本后）

重新启动后观察日志，卡住的步骤会直接显示：

| 日志停在哪 | 对应问题 | 处理 |
|---|---|---|
| `init_db step: create_all` | 数据库锁（其他进程占用）或磁盘极慢 | `lsof <db路径>` 查占用进程；检查磁盘 IO |
| `backfill_strategy_run_types: processed N/M` 持续不推进 | 慢盘 + 大库（**仅首次**，之后有标记跳过） | 等待完成即可，下次启动秒过 |
| `trading calendar fetch attempt X/Y failed` | 服务器无法访问 ciis.com.hk | 不影响启动（后台线程）；检查出网或代理 |
| `app startup: fail_stale_running_runs` 长时间 | 日历月份缺失触发同步拉取 | 确认 `backend/app/data/trading_calendar.json` 已随代码部署 |
| 全部步骤日志已输出但仍无 "Application startup complete" | uvicorn 层问题 | 提供完整日志进一步排查 |

另注意：`--reload` 是开发模式（WatchFiles 监控 + 双进程），生产建议去掉 `--reload` 并配合 systemd/supervisor 管理进程。

## 变更文件

修改（3）：`app/main.py`（分步日志 + 日志配置 + 预热后台化）、`app/db/database.py`（init_db 分步日志 + schema_meta + 回填一次性化）、`app/services/trading_calendar_service.py`（拉取参数收紧 + 失败日志 + logger 定义）

新增（1）：`tests/test_startup_blocking.py`（4 个用例）；适配 `tests/test_schedule_timezone.py`（2 处重试次数断言）
