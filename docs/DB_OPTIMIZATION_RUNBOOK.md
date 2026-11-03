# Aniu 数据库优化执行手册（阶段 A–E）

> 线上实测基线（2026-11-03）：主库 4.71GB / WAL 359MB / 2059 次运行 /
> 单次运行载荷 5–9MB / IO PSI full 85% / `runs-feed?limit=100` 频繁超时。
> 根因不是碎片化，而是 `strategy_runs` 承载了 6MB/次的原始 LLM 与工具载荷，
> 且列表、概览、账户兜底、每日报告都在整行读取它。

## 阶段 A：解除 I/O 堵塞（已完成，纯运维动作）

已经做过的事（无需重复）：清掉卡死的全表扫描进程、`wal_checkpoint(TRUNCATE)`。
复现同样状态时的检查顺序：

```bash
cat /proc/pressure/io                 # full avg10 > 20% 说明磁盘被打满
ps -eo pid,stat,etime,cmd | grep -E "sqlite3|python -c" | grep -v grep
ls -l backend/data/aniu.sqlite3-wal   # 超过几百 MB 说明 checkpoint 被长读事务饿死
```

处置原则：先杀掉不是主服务的长时间全表扫描（确认命令行与 PID），再做
`PRAGMA wal_checkpoint(PASSIVE)` → 无阻塞才 `TRUNCATE`。不要 `pkill -f`，
不要为了清 WAL 删 `.wal`/`.shm`。

## 阶段 B：读取瘦身（代码已完成）

| 接口 | 改造前 | 改造后（实测） |
|---|---|---|
| `GET /runs-feed?limit=100` | 整行 100 条 ≈ 400–600MB 逻辑读取，330ms（热）/ 超时（冷） | 窄列投影，**0.5ms**，不读任何大 JSON |
| `GET /runtime-overview` | 最多 1000 条整行 | 窄列 + SQL 聚合 |
| `GET /account` | 无条件扫最近 20 条整行 | 只在远端失败时惰性兜底 |
| 自动化会话历史 | 最坏 1984 条全量再切片 | SQL `LIMIT` 下推 |
| `GET /runs/daily-report` | `trade_orders` N+1（1+50 次查询） | 一次批量查询 |
| 运行分页 | `started_at` 排序 + `id` 游标（会漏行） | `(started_at, id)` 双字段游标 |

新增统计快照列（`input_tokens/output_tokens/total_tokens/api_call_count/
executed_trade_count`）：运行收尾时写入，历史行用脚本回填。

## 阶段 C：原始载荷冷表 + gzip（代码已完成）

新表 `strategy_run_payloads(run_id, payload_type, payload_data BLOB, ...)`，
四个大字段（`llm_request / llm_response / skill / decision`）压缩后存这里：

- **冷表是唯一持久化位置（DB-01）**：运行收尾时载荷只在内存里用于算
  统计，随即写入冷表；写完整之后用 `set_committed_value` 把内存视图置空，
  **热表四个字段保持 SQL NULL，不再双写**。冷表写入异常/不完整时则保留
  热表副本兜底（宁可热表变大，也不丢审计载荷），且冷表插入与热表字段在
  同一个事务里，失败一起回滚。
- **双读**：详情/报告/账户兜底优先冷表，未回填的历史行回退旧字段；
- 冷表行带 `original_size / compressed_size / sha256`，可离线校验。

> 这一点是"长期不复发"的关键：如果继续双写，历史迁移做完之后热表仍会以
> 约 130MB/天、3.9GB/月 的速度重新膨胀，本次瘦身只是短期收益。

线上实测压缩率（5 条真实载荷样本）：**原始 27.4MB → gzip 4.7MB（17.2%，约 5.8 倍）**。
按此推算，4.7GB 主库迁移 + 清空旧字段 + VACUUM 后约 **0.8–1.2GB**。

## 阶段 D：完整性与耐久性（代码已完成，需执行一次表重建）

1. **外键默认开启**：连接事件 `PRAGMA foreign_keys=ON`（`foreign_key_check` 已验证零违规）。
2. **补外键**：`trade_intents.run_id/action_id`、`self_select_intents.run_id`
   原本是裸 INTEGER；ORM 模型已同步声明 `ForeignKey(..., ondelete="SET NULL")`，
   但**线上库**需要重建表才能补上约束：

   ```bash
   .venv/bin/python -m scripts.migrate_db rebuild-intent-fks   # 默认自动备份
   ```

   重建脚本：读原 DDL → 末尾补 FK 子句 → 建新表 → 拷数据 → 行数校验 →
   换名 → 重建原索引 → `foreign_key_check`，失败即 ROLLBACK。
   `--skip-backup` 才会跳过备份（危险，正常不要用）。

3. **提交耐久性**：`SQLITE_SYNCHRONOUS=NORMAL|FULL`（默认 NORMAL）。
   交易意图 + 远端单号是关键的断电敏感写入，建议压测后切 `FULL`。
4. **迁移版本化**：`schema_meta.schema_version`（`scripts.migrate_db status` 可查）。

## 阶段 E：维护与生命周期（脚本已完成）

```bash
.venv/bin/python -m scripts.db_maintenance analyze      # ANALYZE + PRAGMA optimize
.venv/bin/python -m scripts.db_maintenance checkpoint   # WAL 收敛
.venv/bin/python -m scripts.db_maintenance report       # 行数/压缩率/索引覆盖提示
.venv/bin/python -m scripts.db_maintenance retention \
    --payload-days 180 --tool-log-days 90 --memory-activity-days 365   # 默认 dry-run
.venv/bin/python -m scripts.db_maintenance vacuum --yes # 备份 + checkpoint + VACUUM
```

保留策略**只**清理：冷表载荷、工具日志、记忆活动日志。交易事实
（`trade_orders` / `trade_intents` / `self_select_intents`）、运行结论
（`analysis_summary` / `final_answer`）、执行动作与交易记录**永不**清理。

### 索引复核

`report` 会列出"单列索引已被复合索引前缀覆盖"的候选，删除前必须用
`EXPLAIN QUERY PLAN` 复核实际查询是否仍走覆盖索引。

## 完整执行清单（按此顺序，失败即停）

```bash
cd /home/test/Aniu-main
git status --short && git pull --ff-only && git rev-parse HEAD   # 记录 SHA

# ── 阶段 0：停服（screen 会话里 Ctrl+C，确认调度线程退出、无第二个实例）
cd backend
set -Eeuo pipefail

# ── 阶段 1：基线 + 备份
.venv/bin/python -m scripts.migrate_db status
.venv/bin/python -m scripts.migrate_db backup
.venv/bin/python -m scripts.db_maintenance report
df -h .                                  # 需要 ≥ 3 份备份的空间（约 15GB）

# ── 阶段 2：显式应用结构（加列 + 组合索引 + 冷表），不必靠启动 uvicorn
.venv/bin/python -m scripts.migrate_db apply

# ── 阶段 3：载荷迁移（5 条/批、批间 1 秒，8GB 机器别用 20/0.2）
.venv/bin/python -m scripts.migrate_run_payloads backfill --batch-size 5 --sleep 1
.venv/bin/python -m scripts.migrate_run_payloads verify     # 含"旧字段覆盖度"检查
.venv/bin/python -m scripts.migrate_run_payloads inspect

# ── 阶段 4：历史指标回填（必须在 clear-legacy 之前！）
.venv/bin/python -m scripts.backfill_run_metrics --batch-size 5 --sleep 1

# ── 阶段 5：清空热表旧字段（逐字段比对一致才清）
.venv/bin/python -m scripts.migrate_run_payloads clear-legacy --batch-size 5 --sleep 1

# ── 阶段 6：意图外键重建（自动备份）
.venv/bin/python -m scripts.migrate_db rebuild-intent-fks

# ── 阶段 7：统计信息 + 空间回收
.venv/bin/python -m scripts.db_maintenance analyze
.venv/bin/python -m scripts.db_maintenance vacuum --yes
.venv/bin/python -m scripts.db_maintenance checkpoint

# ── 阶段 8：迁移后体检，然后才启动 uvicorn
.venv/bin/python -m scripts.migrate_db status
.venv/bin/python -m scripts.db_maintenance report
```

**顺序红线**：`backfill_run_metrics` 在 `clear-legacy` 之前；`vacuum` 在
`clear-legacy` 之后；`vacuum` 期间不得有生产读写；迁移全程不启动 uvicorn。

### 重启后先做一次手动分析，再恢复调度

不要一次性恢复 23 条定时任务。先起服务 + 只跑一次手动分析，确认：

1. 新运行只写冷表：`SELECT SUM(CASE WHEN llm_request_payload IS NOT NULL THEN 1 ELSE 0 END) FROM strategy_runs WHERE id > <迁移前最大 id>;` → 期望 **0**；
2. 该运行的冷表有 4 类载荷、压缩率正常（`inspect` 可看）；
3. 运行列表秒开、详情能从冷表完整恢复（含工具明细/API 明细）；
4. Token、API 调用次数、交易次数显示正确（来自统计快照列）；
5. WAL 能正常 checkpoint（`db_maintenance checkpoint` 无 busy）；
6. 观察 RSS：空闲 / 一次运行后 / 连续 10 次后 / 打开 100 条列表 / 看一次详情。

全部通过再恢复调度。

## 迁移后验收 SQL

```sql
-- 1) 旧 JSON 是否真的清空（四个 remaining 期望为 0）
--    注意必须用 CASE 转成整数：sqlite3 CLI 里 SUM(布尔) 可能显示成 True/False
SELECT COUNT(*) AS total_runs,
       SUM(CASE WHEN llm_request_payload  IS NOT NULL THEN 1 ELSE 0 END) AS request_remaining,
       SUM(CASE WHEN llm_response_payload IS NOT NULL THEN 1 ELSE 0 END) AS response_remaining,
       SUM(CASE WHEN skill_payloads       IS NOT NULL THEN 1 ELSE 0 END) AS skill_remaining,
       SUM(CASE WHEN decision_payload     IS NOT NULL THEN 1 ELSE 0 END) AS decision_remaining
FROM strategy_runs;

-- 2) 冷表压缩效果
SELECT payload_type, COUNT(*) records,
       ROUND(SUM(original_size)/1048576.0, 2)   AS original_mb,
       ROUND(SUM(compressed_size)/1048576.0, 2) AS compressed_mb
FROM strategy_run_payloads GROUP BY payload_type;

-- 3) 指标回填情况（NULL 要区分"本来没有"与"回填失败"）
SELECT COUNT(*) total_runs,
       SUM(CASE WHEN input_tokens  IS NULL THEN 1 ELSE 0 END) missing_input_tokens,
       SUM(CASE WHEN output_tokens IS NULL THEN 1 ELSE 0 END) missing_output_tokens,
       SUM(CASE WHEN total_tokens  IS NULL THEN 1 ELSE 0 END) missing_total_tokens,
       SUM(CASE WHEN api_call_count IS NULL THEN 1 ELSE 0 END) missing_api_calls,
       SUM(CASE WHEN executed_trade_count IS NULL THEN 1 ELSE 0 END) missing_trades
FROM strategy_runs;

-- 4) 外键
PRAGMA foreign_key_list(trade_intents);       -- 期望 2 条
PRAGMA foreign_key_list(self_select_intents); -- 期望 1 条
PRAGMA foreign_key_check;                     -- 期望空

-- 5) 完整性（低负载窗口，不要与迁移同时跑）
PRAGMA integrity_check;
```

补充：查看运行详情是**只读**的（冷表补全用 `set_committed_value`，不产生
UPDATE），所以上面第 1 项在有人浏览过详情之后仍然应为 0。

注意：`sqlite3` CLI 新连接的 `PRAGMA foreign_keys` 恒为 0（连接级设置），
验证应用连接要用 SQLAlchemy 连接查，别用 CLI 结果判断是否生效。

## 交易意图链路（安全相关，独立小节）

线上 `trade_intents` 长期为 0 行，排查结论：**代码链路是通的**，注册点在
`skills/mx_core/execution.py::_handle_moni_trade`（真实下单的唯一出口，
全仓 `client.trade()` 仅此一处）。0 行的原因是意图拦截上线（2026-10-30 13:04
上海）之后，到 2026-11-03 为止**没有发生过任何 BUY/SELL/CANCEL**
（`tool_invocation_logs` 最后一条 `mockTrading/trade` 为 10-30 06:41 UTC）。

本次加固：

1. `run_id` 全链路透传：`skills/mx_core/handler.py` 从 `context["run_id"]`
   取值传给 `execute_tool`，规划执行器把 `run_id` 放进工具上下文——修掉了
   "通用 skill 路径（chat/非计划执行）意图 run_id 为 NULL" 的审计缺口；
2. 运行收尾安全网 `_warn_on_unregistered_trade_intents`：执行了买卖却没有
   对应意图行时记 warn（含 run_id / 标的 / 意图 id），让旁路立刻可见；
3. 无 run 上下文下单时 handler 记 warn；
4. `delete_run` 在存在意图时默认拒绝删除（必须 `force=true`，且会保留意图、
   只解除关联），配合阶段 D 的 `ON DELETE SET NULL` 外键；
5. 9 个端到端测试锁死行为：意图注册、重复拦截、超时收敛 UNKNOWN、
   撤单唯一、intent_id→action_id 回填、run_id 透传、安全网告警。

**待产品决策**：`chat` 工具档位也开放了交易工具（`TOOL_PROFILES["chat"]`），
即对话里可以直接下单，但不会产生 StrategyRun / 执行计划 / 快照。当前保留了
功能并加了告警；若要收紧，只需把 `chat` 档位的 `TRADE_TOOL_NAMES` 去掉。

## 回滚

| 场景 | 回滚方式 |
|---|---|
| 冷表写入异常 | 双写阶段热表字段仍在，直接回滚代码即可（读路径自动回退） |
| 清空旧字段后发现问题 | 冷表数据完整，跑 `scripts.migrate_run_payloads backfill` 反向不可用；改用 `UPDATE ... SELECT decompress` 或从 `.bak-*` 恢复 |
| 表重建失败 | `scripts.migrate_db rebuild-intent-fks` 已在事务内且失败即 ROLLBACK；必要时用 `aniu.sqlite3.bak-<ts>` 覆盖 |
| 任何需要整体回退 | `cp aniu.sqlite3.bak-<ts> aniu.sqlite3 && rm -f aniu.sqlite3-wal aniu.sqlite3-shm`（先停服） |

## 验收指标

| 指标 | 改造前 | 目标 |
|---|---|---|
| `runs-feed?limit=100` | 超时 / 330ms（热） | P95 < 100ms |
| 主库体积 | 4.71GB | < 1.5GB（迁移 + VACUUM 后） |
| 冷表压缩率 | — | < 25%（线上实测 17.2%） |
| WAL | 359MB | < 100MB |
| IO PSI full avg10 | 85% | < 5% |
| `foreign_key_check` | 未启用 | 零违规 + 已开启 |
| uvicorn RSS | 4.4GB | < 2GB（需重启后观察） |
