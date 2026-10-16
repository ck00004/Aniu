# Aniu-main P1 功能移植报告

- 移植日期：2026-10-16
- 来源：原版 Aniu v1.0.3（`原版项目/Aniu`）
- 范围：差异分析（UPSTREAM_COMPARISON.md）P1 清单全部 4 项
- 验证：后端 pytest **285 通过**（266 + 19 新增）；前端 node:test **52 通过**；`vue-tsc` 通过；`vite build` 通过；全部新模块导入检查通过

---

## 移植清单

### P1-8 长期记忆系统
- 来源：原版 `business/memories/` + `memory_agent_tools.py` + `memory_repo.py`
- 实现：
  - 新表 `memory_items`（content/reason/version/软删除）+ `memory_activities`（读写审计），create_all 自动建表
  - 新服务 `app/services/memory_service.py`：Agent 路径（`search_for_agent` 关键词 any/all 检索 + 读取活动落审计；`write_for_agent` create/update/delete 三合一）与管理路径（`list_memories`/`list_activities`）；**乐观锁版本控制**（update/delete 必须携带 expected_version，冲突返回明确错误）
  - 新技能 `skills/memory/`（`memory_read`/`memory_write` 两个工具，经 BaseSkill 标准结构接入技能栈，所有 run_type 可用）；**记忆协议随 SKILL.md 正文注入运行系统提示词**（形成买卖判断前先查记忆、值得复用的教训写入、不得保存运行总结/短暂行情/泛泛原则）
  - API：`GET/POST/PUT/DELETE /memories`（409 版本冲突）+ `GET /memory-activities`
- 测试：any/all 检索、版本冲突/更新/软删除、技能运行时端到端、API CRUD 全流程 + 409 + 401、协议注入（6 个用例）

### P1-9 上下文预算 + 溢出恢复
- 来源：原版 `kernel/context_budget.py` + `context_compaction.py`（`_generate_turn_with_context_recovery`）
- 实现：
  - 新模块 `app/services/context_compaction.py`：`compact_messages_for_overflow`（确定性压缩：system 保留、最近后缀按 60% 预算保留、较早工具结果→占位符、较早文本→512 截断、仍超预算从最旧丢弃、单条硬截断兜底）+ `trim_messages_to_budget`（从最旧裁剪到预算内）
  - **agent 循环溢出恢复**：provider 返回上下文溢出错误（`LLMErrorCode.CONTEXT_OVERFLOW`，P0-3 已建立分类）后压缩历史并重试**恰好一次**（第二次溢出直接抛出，避免无限循环）；run 路径使用 `automation_context_window_tokens`，聊天默认 32768
  - **聊天历史 token 预算**：40 条上限之外新增 60K token 预算防线（CJK 感知估算），超预算从最旧裁剪
- 测试：预算内不变、工具结果压缩 + system/最新保留、超预算丢弃最旧、trim 裁剪、溢出恢复（压缩后更小 + 重试成功 + stage 提示）、二次溢出直接抛出（6 个用例）

### P1-10 SSE 断线续传
- 来源：原版 `api/sse/stream_hub.py` 的 seq/replay 设计 + `use-run-snapshot-stream.ts` 的重连状态机
- 实现：
  - 后端：`event_bus` 为每个事件分配**单调递增 event_seq**；`subscribe`/`stream` 接受 `after_seq` 游标（只重放游标之后的事件）；`GET /runs/{id}/events?after_seq=N`
  - 前端 `useRunStream`：跟踪 `lastEventSeq` 游标；流**中断或静默结束（无终态事件）**时带 `?after_seq=` 指数退避重连（500ms 起、8s 封顶、最多 6 次）；401/403/404 不可重试状态直接失败；重连期间 UI 显示"正在重连"提示。同时修复了"流静默结束后 UI 永远停留在 running"的问题
- 测试：后端（单调序号、游标过滤、流续传、API 端点含终态结束）4 个用例；前端（404 不重试、中断后带游标重连续传）2 个用例

### P1-11 每日运行报告 + 证据预算
- 来源：原版 `stages/stage_input.py:100-195` 的确定性证据预算
- 实现：
  - 新服务 `app/services/daily_report_service.py`：`apply_evidence_budget`（**交易/撤单调用为必需证据永不丢弃**（截断 2000）；查询类结果两级截断 4000→512，总量超 24K 从最旧丢弃并记录 omissions）+ `build_daily_report`（上海日历日聚合当日运行：结论/交易/证据/失败原因 + 当日交易汇总 + Markdown 渲染）——是后续"记忆梦境"夜间整理的输入
  - API：`GET /runs/daily-report?date=YYYY-MM-DD`（默认今日上海）
- 测试：必需证据保留、查询丢弃 + omissions、报告聚合（运行/交易/Markdown）、API 端点（4 个用例）

---

## 测试汇总

| 套件 | 结果 |
|---|---|
| 后端 pytest（266 原有 + 19 新增） | **285 通过 / 0 失败** |
| 前端 node:test（含 2 个重连测试） | **52 通过 / 0 失败** |
| vue-tsc 类型检查 | 通过 |
| vite build 生产构建 | 通过 |
| 新模块导入检查 | 通过 |

新增测试文件：`backend/tests/test_p1_port.py`（19 个用例）；前端 `tests/useRunStream.test.ts` 新增 2 个重连用例。
适配的既有测试：`test_chat_api.py` 的事件流 mock 增加 `**kwargs`（适配 after_seq）。

## 变更文件

新增（5）：`app/services/{memory_service,context_compaction,daily_report_service}.py`、`skills/memory/`（SKILL.md + handler + __init__）、`tests/test_p1_port.py`

修改（8）：`app/db/models.py`（2 张记忆表）、`app/schemas/aniu.py`（5 个记忆 schema）、`app/api/router.py`（记忆 5 端点 + daily-report + after_seq）、`app/services/llm_service.py`（溢出恢复 + 窗口参数）、`app/services/chat_session_service.py`（token 预算）、`app/services/event_bus.py`（event_seq + after_seq）、`frontend/src/composables/useRunStream.ts`（重连）、`backend/tests/test_chat_api.py`（mock 适配）

## 未移植（P2 梯队，见 UPSTREAM_COMPARISON.md）

记忆梦境夜间整理（依赖本次的记忆系统 + 每日报告，两者已就绪）、公开行情层（query_kline 等）、多渠道模型、行情总览页、CI/Docker 加固。
