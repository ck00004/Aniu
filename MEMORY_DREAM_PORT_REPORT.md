# Aniu-main 记忆梦境移植报告（P2-12）

- 移植日期：2026-10-16
- 来源：原版 Aniu v1.0.3 `business/dreams/` + `infra/integrations/dream_agent.py` + `dream_agent_tools.py`
- 依赖：P1-8 长期记忆系统、P1-11 每日运行报告（均已就绪并打通）
- 验证：后端 pytest **302 通过**（285 + 17 新增）；前端 52 通过（无前端改动，回归确认）；`vue-tsc` 通过；端到端冒烟通过（真实 DB + mock LLM：梦境执行 → 记忆落库 → 状态完成）

---

## 功能形态

每天（默认上海时间 00:30，可在设置中调整 `automation_dream_time`）自动整理**昨天**的运行报告与长期记忆：Dream 智能体先分页阅读当日报告和全部现有记忆，再自主决定创建、更新或软删除记忆（带乐观锁版本控制），完成后汇报整理结果。也可通过 API 手动触发（已完成/失败的任务会重置重跑）。

## 实现清单

| 组件 | 内容 |
|---|---|
| **DB 模型** | 新表 `memory_dreams`（target_date 唯一索引 → 每日幂等；状态机 pending → running → completed/failed；result/failure_reason/started_at/finished_at）；`app_settings` 新列 `automation_dream_time`（含旧库 ALTER 迁移） |
| **Dream 智能体** | 复用现有 `llm_service.run_agent_with_messages`；新增 **`tools_override` 参数**（agent 循环精确控制模型可见工具，绕过技能栈全量工具）；固定整理指令（移植自原版 `_dream_request`）注入系统提示词 |
| **工具白名单（4 个）** | `dream_read_reports`（分页读当日运行报告，走每日报告服务）+ `memory_list`（分页读全部记忆）+ `memory_read`（关键词检索）+ `memory_write`（create/update/delete 带乐观锁）；执行器对白名单外的任何工具（含交易工具）直接拒绝并返回中文原因 |
| **服务** | `memory_dream_service`：`create_or_get`（按日期幂等）、`execute_dream`（状态机 + 互斥锁防并发）、`run_dream_async`（手动触发，后台线程）、`process_due_dream`（调度钩子：过整理时间且当日无任务时触发一次；已存在（含失败）不自动重试——与原版一致，重跑走手动入口）、`fail_stale_running_dreams`（启动恢复） |
| **调度接线** | `scheduler_service` 循环每轮调用 `process_due_dream()`（异常隔离，不影响策略调度） |
| **启动恢复** | `main.py` lifespan：遗留 running 的梦境任务收敛为 failed（"应用进程中断，未自动重试，请确认后手动重试"） |
| **API** | `GET /memory-dreams`（列表）、`GET /memory-dreams/{id}`（详情）、`POST /memory-dreams/run?date=`（手动触发，默认昨天）、`DELETE /memory-dreams/{id}`（running 状态 409 拒绝） |

## 与原版的差异（单进程适配）

- 砍掉 DB 租约/心跳/execution_fence（原版为多进程 worker 设计）——分支单进程部署用**互斥锁**防并发 + 启动恢复兜底，语义等价。
- Dream 阶段模型配置复用分支的全局 LLM 设置（原版支持按阶段独立选模型，属 P2-14 多渠道范畴，未移植）。
- `dream_read_reports` 读的是每日运行报告（P1-11 的证据预算产物）而非逐条 Run 报告记录——数据等价且已含交易/证据预算。

## 测试（17 个新回归测试）

- **状态机**：按日期幂等创建、终态任务重置重跑
- **工具白名单**：恰好 4 个工具；交易工具被拒绝；报告/记忆列表/检索/写入全链路；版本冲突透传
- **执行**：成功落 result（验证 tools_override=4 工具、dream run_type、指令注入、用户消息含日期与 expected_version 要求）；失败落 failure_reason；未配置 LLM 明确报错；非 pending 不执行；互斥锁防并发
- **调度**：未到时间不触发；过时间触发一次且整理对象为昨天；已存在不重复触发
- **恢复**：启动时 running → failed
- **API**：手动触发→列表→详情→删除全流程 + 404/409/401
- **tools_override 传导**：agent 循环的请求 payload 精确只含 4 个白名单工具（即使 run_type=analysis）
- **端到端冒烟**：真实 DB（含列迁移）→ 手动触发 → mock LLM 中调用白名单工具写记忆 → 状态 completed + 记忆可检索

测试中发现并修复的问题：`automation_dream_time` 最初只加到 schema 漏了 ORM 模型字段，`getattr` 静默落到默认值——"未到时间不触发"测试暴露后补齐（模型 + 迁移 + schema 三处对齐）。

## 变更文件

新增（2）：`app/services/memory_dream_service.py`、`tests/test_memory_dream.py`

修改（7）：`app/db/models.py`（MemoryDream 表 + AppSettings 字段）、`app/schemas/aniu.py`（4 个 schema + dream_time 字段）、`app/db/database.py`（列迁移）、`app/api/router.py`（4 个端点）、`app/services/llm_service.py`（tools_override 参数）、`app/services/scheduler_service.py`（调度钩子）、`app/main.py`（启动恢复）

## 记忆系统全景（本次移植后完整闭环）

```
运行（分析/交易）──记忆协议提示词──> memory_read 查询经验 / memory_write 写入教训
       │
       └──> 每日运行报告（P1-11，证据预算）
                    │
每晚 00:30 ──> 记忆梦境（P2-12）──读当日报告 + 全部记忆──> 自主增删改（乐观锁）
                    │
                    └──> 整理结果落库，次日运行即可检索到沉淀后的经验
```

## 后续（未移植）

- 梦境/记忆的前端管理页（原版 memories 页面，Vue 重写属 P2-16）
- Dream 阶段独立模型配置（依赖 P2-14 多渠道）
- 其余 P2 项：公开行情层、OpenAI 兼容推断、CI/Docker 加固（本轮按用户指示聚焦梦境，未动）
