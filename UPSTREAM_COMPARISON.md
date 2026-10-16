# Aniu 原版（v1.0.3）与 Aniu-main 分支功能差异分析

- 分析日期：2026-10-16
- 对比对象：原版 `原版项目/Aniu`（github.com/AnacondaKC/Aniu，v1.0.3）vs 本分支 `Aniu-main`（基于 v0.3.x 分叉）
- 分析方式：三路并行代码深读（智能体引擎+LLM 层 / 业务层+市场数据 / 前端+工程化），全部结论基于实际代码（文件:行号），非 README 推断

## 〇、重要背景：两边已不是一个代码库

分支基线是 v0.3.x（`backend/app` + Vue，与当前分支同构）。上游在 v0.3.x 之后做了一次**彻底重写**（10 万行以单提交导入）：React 19 前端 + 分层后端（`agent/llm/business/infra/stock_api/api/bootstrap`），规模 6.1 万行后端 + 2.3 万行前端（分支为 1.6 万 + 0.9 万）。因此**无法做增量合并**，只能按功能逐项移植。产品定位也分化了：原版是"任务运行型工作台"（无聊天），分支是"运行 + 多会话聊天"混合形态。

---

## 一、总体对比

| 维度 | 原版 v1.0.3 | 分支 Aniu-main |
|---|---|---|
| 智能体 | 两阶段（Run 研究+交易直出报告 → Summary 转 HTML）+ 独立 Dream 记忆整理智能体 | 单阶段（新闻预取 → agent 循环 → 一致性校验 → 计划执行 → 落账） |
| LLM | 双协议（OpenAI 兼容 + Anthropic）、多渠道、按阶段（Run/Summary/Dream）选模型、快照冻结、连通性检测、thinking effort | 单一全局 OpenAI 兼容配置 |
| 上下文管理 | token 预算驱动压缩 + provider 溢出被动恢复 + append-only journal | 摘要压缩（自动化会话）+ 固定 40 条裁剪（聊天） |
| 中止 | 全链路 abort（API 端点 → job 取消 → 流级取消） | 无（客户端断开仅取消当前工具调用） |
| 市场数据 | **双轨**：结构化公开数据（东财/腾讯/新浪，免密钥三源回退，14 类工具）+ 妙想 NL 查询 | **单轨**：全部走妙想自然语言接口 |
| 记忆 | 长期记忆库（乐观锁版本控制）+ 记忆梦境夜间整理智能体 | 无独立记忆系统 |
| 运行可靠性 | DB 租约 worker + 写工具幂等台账 + 交易时段写门禁 | 计划-执行分离 + 草稿去重 + 状态机（本轮加固后） |
| 账户 | DB 持久化缓存 + 交易时段定时刷新 + stale 降级 | 内存 30s 缓存 + 快照回退 + **FIFO 已平仓盈亏**（原版没有） |
| 聊天 | **没有** | 多会话 + 附件 + 流式（分支独有） |
| 技能系统 | **没有** | builtin + workspace zip 导入 + 启停持久化（分支独有） |
| SSE | 断线续传（stream_id + event_seq + after_seq + checkpoint） | 简单事件流 + heartbeat |
| 工程化 | CI（OpenAPI 契约门禁 + 依赖审计）、非 root Docker + HEALTHCHECK、一行安装 | 无 CI/安装脚本 |

---

## 二、原版明显更好的部分（机制级差异）

### 2.1 智能体引擎与 LLM 层（最值得借鉴的一块）

| 机制 | 原版实现 | 分支现状 | 差距 |
|---|---|---|---|
| **上下文压缩** | token 预算驱动（`context_window − 输出预留 − 5% 安全边际`），保留最近 turn 而非固定条数，分块摘要 + 单条二分分割，前次摘要折叠，**provider 返回溢出后强制压缩并重试一次**（`agent/kernel/context_compaction.py:90-195`、`context_budget.py:22-97`） | 聊天固定 40 条裁剪；自动化会话按条数触发压缩 | 原版按 token 科学裁剪且能从溢出中自愈，分支长会话仍有溢出风险 |
| **LLM 重试** | 双层：驱动层尊重 `Retry-After` 头（ms/s/HTTP 日期三种格式）+ `x-should-retry` 覆盖（`llm/errors.py:83-112`）；kernel 层错误分类（rate_limit/timeout/network/5xx/empty_response）3 次退避（`kernel/llm_runtime.py:435-599`） | 仅 5xx/超时简单重试 | 原版对限频/网关行为的处理精细得多 |
| **usage 统计** | 每响应归一化 Usage（input/output/cache_read/cache_write/reasoning），流式逐 chunk 合并（`llm/usage.py`） | 仅记最后一个响应（多轮累计少记） | 分支的 token 统计失真 |
| **全链路中止** | `POST /runs/{id}/abort` → job 级取消 → 进程内 signal → 流级取消（每个 chunk 可中断）（`llm/abort.py:25-61`、`runs/abort.py`） | 无中止端点，运行失控只能等超时 | 分支最明显的功能缺口之一 |
| **工具循环文本防闪烁** | 中间轮次文本先缓冲，仅最终轮发布（`llm_runtime.py:638-657`） | 中间说明文本会作为 final 流出（已部分修复落库语义，但客户端仍会看到两段） | 体验差距 |
| **token 估算** | ASCII/4 + CJK 全计（对中文友好）+ 构建 provider 可见 payload（含工具 schema）再估算（`llm/context.py:32-108`） | (len+3)//4，中文低估 2.5-4 倍 | 分支压缩触发时机系统性偏晚 |
| **工具并行执行** | sequential 工具强制串行，其余信号量并行（默认 10）（`actors/tool_executor.py:284-337`） | 串行 | 长运行可提速 |
| **多渠道 + 按阶段模型** | 渠道 CRUD + models.dev 目录 + 运行时快照冻结（密钥不进快照）（`business/settings/channels.py`、`stages.py`） | 单一全局配置 | 可"Run 用强模型、Summary 用便宜模型"；单用户可简化 |

### 2.2 业务与数据层

| 机制 | 原版实现 | 分支现状 |
|---|---|---|
| **长期记忆系统** | `memory_items`（乐观锁版本）+ `memory_activities` 审计 + `memory_read/write` 工具 + 提示词注入"记忆协议"（形成判断前先查经验，值得复用的教训写入）（`business/memories/`、`run_stage.py:13-29`） | 无——交易经验不跨 run 累积 |
| **记忆梦境（夜间整理）** | 每日 00:30 调度，Dream 智能体分页读当日全部运行报告 + 现有记忆，自主增删改（带 expected_version 乐观锁），工具白名单仅 4 个只读/写记忆工具（`infra/integrations/dream_agent.py:97-137`） | 无 |
| **结构化公开行情** | 东财/腾讯/新浪免密钥三源回退 + 限频门（东财 push2 2 并发/0.5s 等）+ TTL 缓存 + single-flight + 结果边界（64K 渐进收缩）（`stock_api/public/`，约 5000 行自包含） | 全部走妙想 NL 接口——**每次行情查询消耗配额**，且结果格式不可控 |
| **统一 K 线** | `query_kline`：个股+指数、1m~月线、复权、区间、指数别名解析（"上证指数"→000001.SH） | 无结构化 K 线 |
| **交易预检** | 买入前核对可用资金、卖出前核对可卖数量（含**在途委托占用推算**）、撤单前核对可撤状态（`mx_agent_tools.py:314-389`） | 无预检——废单/超卖依赖远端拒绝 |
| **交易时段写门禁** | 写工具 + `requires_market_open` 在非交易时段直接 block 并返回中文原因给模型（`agent_runner.py:459-471`） | 无——闭市委托直接下发失败 |
| **工具调用日志** | `stock_api_call_logs` 表记录每次上游调用（provider/参数/状态/耗时/响应大小/错误分类）+ 设置页查询（`business/stock_api_logs/`） | 无——上游调用不可观测 |
| **账户缓存** | DB 4 表持久化 + 交易时段整点定时刷新 + 失败降级 stale + 10s 节流 | 内存 30s（重启即丢） |
| **每日运行报告** | Run 产出 Markdown 报告 + Summary 阶段带**确定性证据预算**（trade/cancel 不可裁剪，查询类先截断再丢弃，记录 omissions）转 HTML（`stages/stage_input.py:100-195`） | 无独立报告产物 |

### 2.3 前端与工程化

| 机制 | 原版实现 | 分支现状 |
|---|---|---|
| **SSE 断线续传** | stream_id + event_seq 游标 + after_seq 续传 + checkpoint 快照 + 指数退避 + 乱序检测重连 + rAF 批量合并 delta（`use-run-snapshot-stream.ts`、`api/sse/stream_hub.py`） | 手写解析，断线即丢流 |
| **过程轨迹 UI** | 深度思考行（可展开）、工具调用行（来源徽章/参数/耗时/状态）、记忆工具行、交易门禁行、上下文压缩行，默认收起前 5 条（`run-workbench/timeline/stage-events.tsx`） | 流式卡片无过程轨道 |
| **行情/账户总览页** | 指数 sparkline + 分时成交额柱 + 涨跌家数 + 四排行 + 热点 + 要闻，子资源独立降级；账户四卡 + 持仓/委托表 | `/overview` 仅运行统计 |
| **交易时段感知轮询** | 盘中 5s、休市自动降到"距下次开盘"（含节假日跳周末）（`dashboard-page.tsx:195`） | 无 |
| **工程化** | CI 双 job + **OpenAPI 契约门禁**（重新生成 schema 后 git diff，漂移即红）+ 依赖审计（豁免带到期日）；非 root Docker（uid 10001 + entrypoint 降权 + HEALTHCHECK + no-new-privileges）；curl 一行安装（幂等升级 + 哈希锁定依赖） | 无 CI/安装脚本；Docker 无健康检查 |

---

## 三、分支独有优势（原版没有，应保留并继续强化）

1. **多会话聊天 + 附件**（`/chat`）：原版完全没有对话式界面，产品形态是"定时任务运行型"。这是分支最大的差异化面。
2. **技能系统**：builtin + workspace zip 导入 + 启停持久化 + 技能栈运行时——原版没有对应物，是分支的扩展性核心。
3. **计划-执行分离 + 一致性校验**：变更工具先经 planning executor 生成草稿 → 声称/实际一致性校验 → 统一执行落账。原版没有"计划"概念（工具即时执行，靠幂等预留 + 门禁边执行边防护）。对交易系统，显式计划+校验是更直观的安全模型——本轮重复委托加固（草稿去重 + 状态过滤 + 重入防护）进一步强化了这条路线。
4. **FIFO 已平仓盈亏计算**：原版只有 MX 返回的 day_profit/total_profit。
5. **金十/财联社资讯主动注入**：分块 LLM 分析后保证每次运行都有资讯背景；原版资讯是被动的查询工具（东财/新浪源），依赖 Agent 主动调用。两者可并存（注入保证下限，工具提供深度）。
6. **runtime overview 统计聚合**：分支的运行统计视角原版没有。

---

## 四、移植建议（按价值/成本/依赖排序）

### P0 — 高价值、低成本、零依赖（建议立即做，合计约 1000 行）

| # | 移植项 | 来源 | 说明 |
|---|---|---|---|
| 1 | **交易预检** | `mx_agent_tools.py:314-389` | 买入查资金/卖出查可卖（含在途占用推算）/撤单查状态。分支已有持仓/余额查询，纯逻辑复用，直接减少废单 |
| 2 | **交易时段写门禁** | `agent_runner.py:459-471` | 工具元数据 `requires_market_open` + authorizer 回调，约 30 行，闭市直接 block 交易工具 |
| 3 | **LLM 错误分类 + 重试** | `llm/errors.py` + `retry.py` | 独立纯函数，尊重 Retry-After，套在分支的 llm_service 上 |
| 4 | **usage 归一化累计** | `llm/usage.py` | 修复"仅记最后响应"，agent 循环每轮累加 |
| 5 | **AbortSignal + 中止端点** | `llm/abort.py`（64 行）+ `runs/abort.py` | API abort → 进程内 signal → 循环/工具前检查；补上分支最大的功能缺口 |
| 6 | **token 估算修正** | `llm/context.py:32-39` | ASCII/4 + CJK 全计，一行级改动，让压缩触发时机正确 |
| 7 | **工具调用日志** | `stock_api_logs/` | 妙想客户端加调用钩子 + 一张表 + 查询 API，上游可观测 |

### P1 — 高价值、中成本（少量依赖）

| # | 移植项 | 来源 | 依赖 |
|---|---|---|---|
| 8 | **长期记忆系统** | `business/memories/` + `memory_agent_tools.py` | 仅本地 DB + 现有工具框架；纯增量。含乐观锁版本控制和"记忆协议"提示词注入 |
| 9 | **上下文预算 + 压缩 kernel** | `kernel/context_budget.py` + `context_compaction.py`（668 行自包含纯算法） | 依赖 #6；把固定 40 条升级为 token 预算 + 溢出恢复 |
| 10 | **SSE 断线续传** | `stream_hub.py` 的 seq/replay/checkpoint 设计 | 需事件加 event_seq 字段 + 前端配合 after_seq 重连 |
| 11 | **每日运行报告 + 证据预算** | `stages/stage_input.py:100-195` | 也是 #12 Dream 的输入前提 |

### P2 — 高价值、高成本（按需选摘）

| # | 移植项 | 说明 |
|---|---|---|
| 12 | **记忆梦境夜间整理** | 依赖 #8 + #11；Dream 智能体复用分支 agent 框架，工具白名单 4 个；单进程可砍掉租约机制 |
| 13 | **结构化公开行情层（先 query_kline + stock_quote）** | `stock_api/public/` 约 5000 行自包含（仅依赖 httpx），东财/腾讯/新浪免密钥；**摆脱妙想配额依赖**是最大收益；必须保留限频门和 Referer 头 |
| 14 | **多渠道模型管理（简化版）** | 单用户可砍掉按阶段与快照冻结，只做"多渠道 + 默认模型"；OpenAI 兼容推断（o1/o3/gpt-5 自动 max_completion_tokens）单独就值得抄 |
| 15 | **账户 DB 持久化缓存 + 定时刷新** | 与现有 30s 内存缓存共存 |
| 16 | **行情/账户总览页** | Vue 重写（不能抄 React 代码），可拆分渐进：先指数卡 + 排行 |
| 17 | **工程化**：CI（含 OpenAPI 契约门禁）、Docker 加固（非 root/HEALTHCHECK/no-new-privileges）、一行安装 | 与框架无关，照抄模式即可 |

### 不建议移植

- **两阶段 Run/Summary 编排**：分支的单阶段 + 一致性校验 + 计划执行更贴合交易域；原版 Summary 本质是 HTML 展示层。
- **DB 租约 worker / 写工具幂等台账**：防多进程重复交易的设计；分支单进程部署 + 本轮加固（草稿去重/状态过滤/调度推进）已覆盖主要风险，收益有限。
- **append-only session journal**：分支是单次运行模型，多轮投影价值低。
- **完整多阶段模型配置（Run/Summary/Dream 三阶段独立）**：分支没有阶段概念，强搬需要先引入阶段架构，成本不成比例。

### 依赖关系图

```
#6 token 估算 ──→ #9 预算压缩
#5 AbortSignal ──→ 全链路中止（API 端点）
#8 记忆系统 ──→ #12 记忆梦境（还需 #11 报告）
#11 每日报告 ──→ #12 记忆梦境
#13 公开行情层 ──→ #16 行情总览页
#1/#2 交易预检+门禁：独立
```

---

## 五、一句话结论

原版在**引擎质量**（上下文预算压缩、双层重试、全链路中止、断线续传）和**数据面**（免密钥结构化行情、长期记忆、夜间整理、工具日志）上领先一个代差；分支在**产品形态**（多会话聊天、技能系统）和**交易安全模型**（计划-执行分离 + 一致性校验 + 本轮重复委托加固）上有原版没有的优势。建议按 P0 清单先补引擎短板（约 1000 行、零依赖、全部可直接落地），再按需引入记忆系统与公开行情层——后两者是把分支从"单次运行智能"升级为"跨运行经验积累 + 数据自主"的关键。
