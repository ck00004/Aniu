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

- **双写**：运行收尾时同时写热表旧字段与冷表；
- **双读**：详情/报告/账户兜底优先冷表，未回填行回退旧字段；
- 冷表行带 `original_size / compressed_size / sha256`，可离线校验。

线上实测压缩率（5 条样本，真实载荷）：**原始 27.4MB → gzip 4.7MB（17.2%，约 5.8 倍）**。
按此推算，4.7GB 主库迁移 + 清空旧字段 + VACUUM 后约 **0.8–1.2GB**。

执行顺序（低峰期；每步可中断、可重复）：

```bash
cd /home/test/Aniu-main/backend

# 0) 先备份 + 体检
.venv/bin/python -m scripts.migrate_db backup
.venv/bin/python -m scripts.db_maintenance report

# 1) 回填冷表（读 4GB 老数据，分批 + 批间休眠，避免卡住 WAL）
.venv/bin/python -m scripts.migrate_run_payloads backfill --batch-size 20 --sleep 0.2

# 2) 校验（sha256 / 大小 / JSON 一致性，异常会返回非 0 并中止）
.venv/bin/python -m scripts.migrate_run_payloads verify

# 3) 清空热表旧字段（逐行比对一致才清；不一致会跳过并告警）
.venv/bin/python -m scripts.migrate_run_payloads clear-legacy --batch-size 20 --sleep 0.2

# 4) 回收磁盘（WAL 模式下 VACUUM 后必须 checkpoint，脚本已内置）
.venv/bin/python -m scripts.db_maintenance vacuum --yes
```

## 阶段 D：完整性与耐久性（代码已完成，需执行一次表重建）

1. **外键默认开启**：连接事件里 `PRAGMA foreign_keys=ON`（已用
   `foreign_key_check` 验证历史数据零违规）。
2. **补外键**：`trade_intents.run_id/action_id`、`self_select_intents.run_id`
   原本是裸 INTEGER。SQLite 不支持 `ADD CONSTRAINT`，需要重建表：

```bash
.venv/bin/python -m scripts.migrate_db rebuild-intent-fks    # 自动备份 + 行数校验 + foreign_key_check
```

外键用 `ON DELETE SET NULL`：删除运行时意图记录必须保留（标的/方向/数量/
远端单号/状态是交易事实），只解除关联；应用层 `delete_run` 另加确认门槛——
存在意图时默认拒绝删除，必须显式 `force=true`，并记 warn 日志。

3. **提交耐久性**：`SQLITE_SYNCHRONOUS=NORMAL|FULL`（默认 NORMAL）。
   交易意图 + 远端单号属于关键写入，建议在压测后切 `FULL`：

```bash
SQLITE_SYNCHRONOUS=FULL   # .env
```

4. **迁移版本化**：`schema_meta.schema_version` 记录结构版本
   （`scripts.migrate_db status` 可查）。目前是"幂等脚本 + 版本标记"的
   轻量方案，不用 Alembic 也能追溯；后续若要引入 Alembic，这张表可直接
   作为 baseline。

## 阶段 E：维护与生命周期（脚本已完成）

```bash
# 统计信息（让查询计划准确；建索引/大批写入后跑一次）
.venv/bin/python -m scripts.db_maintenance analyze

# WAL 收敛
.venv/bin/python -m scripts.db_maintenance checkpoint

# 体检报告：各表行数、冷表压缩率、复合索引覆盖提示
.venv/bin/python -m scripts.db_maintenance report

# 保留策略（默认 dry-run，确认后加 --apply）
.venv/bin/python -m scripts.db_maintenance retention \
    --payload-days 180 --tool-log-days 90 --memory-activity-days 365
```

保留策略**只**清理：冷表原始载荷、工具调用日志、记忆活动日志。
交易事实（`trade_orders` / `trade_intents` / `self_select_intents`）、运行结论
（`analysis_summary` / `final_answer`）、执行动作与交易记录**永不**进入清理。

### 索引复核

`report` 会列出"单列索引已被复合索引前缀覆盖"的候选：

```
strategy_runs.ix_strategy_runs_status  ← ix_strategy_runs_status_started_id(status, started_at, id)
strategy_run_actions.ix_strategy_run_actions_run_id ← (run_id, sequence_no) / (run_id, status)
```

删除前必须用实际查询做 `EXPLAIN QUERY PLAN` 复核（例如"只按 status 过滤"
是否仍走覆盖索引）。不要为了省几十 KB 盲删索引。

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
