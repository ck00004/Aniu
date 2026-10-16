# Aniu-main 完整逻辑审计报告

- 审计日期：2026-10-15
- 审计范围：Aniu-main 全部前后端源码（后端约 1.6 万行，前端约 0.9 万行），不含安全漏洞审计
- 审计方式：9 路并行分层审计（后端核心/数据层、aniu_service 前后两半、API 契约、聊天与 LLM、执行调度链路、技能系统、数据源服务、前端），关键发现均经代码逐行核实
- 严重度定义：P0 = 核心功能错误或数据损坏；P1 = 特定条件下功能错误；P2 = 边缘条件/健壮性缺陷；P3 = 可维护性/死代码

---

## 一、总体结论

项目整体架构分层清晰（接口层/业务层/数据层/技能层/展示层），防御性编程意识较强（`_assume_utc`、`isinstance` 分层校验、事件发射吞异常、失败回退缓存快照），测试覆盖了调度时区、技能管理、聊天接口等关键行为。**未发现 P0 级"必然发生的数据损坏"**，但存在 **18 项 P1 级问题**，集中在五个系统性模式上：

1. **交易执行链路的防护不对称**（最严重）：一致性校验子系统可在分析任务中自动下达真实模拟委托、可产生重复委托、followup 路径的真实交易不入台账；MX 接口 HTTP 200 错误体不检测导致失败交易被记为成功。
2. **时区纪律执行不彻底**：全项目以上海时区为锚，但存在 4 处破口（run 日期过滤、CLS 快讯时间渲染、启动预热日期、前端今日交易统计），其中两处直接产生错误数据。
3. **上下文生命周期断裂**：用户聊天会话无任何裁剪/压缩（自动化路径有完整实现却未复用）；持久会话压缩的摘要生成了却从不注入提示词——压缩实际等于删除上下文。
4. **"一次性"逻辑留在常驻路径**：历史 run 迁移每次启动重写全表、runtime 统计只看最近 100 条、日历缓存永不刷新。
5. **前端异步无竞态防护**：聊天流式写入"当前数组末尾"而非发起请求的目标会话、慢响应覆盖新状态、运行完成后 UI 钉死在实时快照视图。

---

## 二、P1 级发现（18 项）

### A. 交易执行链路（风险最高，建议最优先修复）

#### A1.【P1】交易一致性 autofill 无 run_type 门控：分析任务可自动下达真实模拟委托
- 位置：`backend/app/services/aniu_service.py:4459-4551`（`_enforce_trade_consistency`）；对照 `aniu_service.py:4299`（自选股版本有 `run_type == "trade"` 门控）
- 问题：`_enforce_self_select_consistency` 在分析任务中显式跳过，但 `_enforce_trade_consistency` **没有对称门控**，且 `_run_body` 对所有运行都调用它（`aniu_service.py:2636`）。当最终答案出现非条件式、带数量的交易表述时（注意"建议"不在条件词列表 `conditional_markers` 中，`aniu_service.py:3601`），autofill 会生成 `mx_moni_trade` 计划动作，经 `execution_runner_service.execute_plan` → `client.trade(...)` **真实执行**。
- 完整链条（已核实）：`_enforce_trade_consistency`（无门控）→ `_build_trade_consistency_autofill_tool_calls` → `_materialize_consistency_operations`（`aniu_service.py:3321`）→ `execution_plan_service.execute_tool` 生成 planned draft → `_run_body:2654` 提取 drafts → `execution_runner_service.execute_plan` 真实下单。
- 影响：用户在**未开启交易**的分析任务里，仅因模型答案写了"操作建议：买入贵州茅台(600519) 1000股"，系统就会自动下单。

#### A2.【P1】autofill 去重键含数量/价格，与粗粒度 gap 判定组合导致重复下单
- 位置：`aniu_service.py:3748-3810`（gap 判定仅按 action+目标名）vs `aniu_service.py:3293-3319`（`_build_trade_action_key` 五元组 `(action, symbol, quantity, price_type, price)` 去重）
- 问题：是否触发 autofill 只看"声称的买/卖目标是否存在"（名称级），而补齐操作的查重要求五元组完全一致。模型已计划 `BUY 600519 500股`、答案却写"买入600519 1000股"时，若因另一条缺口触发 autofill，会生成第二笔 1000 股的重复买入并真实执行。
- 影响：特定条件下产生重复真实委托，直接造成资金层面的错误执行。

#### A3.【P1】一致性 followup LLM 调用未传 planning tool_executor：真实交易绕过计划管线且不入台账
- 位置：`aniu_service.py:4402-4409、4576-4583`（followup 调用不传 `tool_executor`）；`llm_service.py:644-676`（默认执行器直接真实执行）；对照 `aniu_service.py:2584-2604`（主运行使用 `_planning_tool_executor`）
- 问题：主运行中变更工具只"生成计划"（无副作用），但两个 `_enforce_*_consistency` 的 followup 分支调 `run_agent_with_messages` 时使用默认执行器，模型若调 `mx_moni_trade` 会**立即真实成交**，且该交易不进 `run.executed_actions`、不写 `TradeOrder`、不进 execution_summary——仅埋在 decision_payload 里。
- 影响：真实交易从运行记录/交易台账中丢失，对账层（reconcile 只对账本地计划 vs 本地执行记录）无法发现。

#### A4.【P1】自选股声称提取把普通中文词当股票名：触发垃圾 autofill 操作并使 run 失败
- 位置：`aniu_service.py:3461-3490`（`_extract_stock_mentions` 的 `[\u4e00-\u9fa5A-Za-z]{2,20}` 匹配行内所有 ≥2 字中文串）；`aniu_service.py:3533-3563`、`4042-4070`
- 问题：stopwords 只覆盖少量固定词。常见排版如"- 贵州茅台(600519)：业绩超预期，估值偏低"会把"业绩超预期""估值偏低"一并推成自选股 add 目标；这些垃圾目标与实际操作不相交 → gap 判真 → autofill 生成"将业绩超预期加入自选股"之类操作 → 真实调用 MX 接口。
- 影响：常见排版即可触发对 MX 接口的垃圾调用；调用失败 → unresolved → 整个 run 被标记 failed；若 MX 端模糊匹配成功还会误改自选股。

#### A5.【P1】MX 交易/撤单不检查响应体业务错误：HTTP 200 错误体时失败交易被记为成功
- 位置：`backend/skills/mx_core/client.py:34-38`（`_post` 只做 `raise_for_status()`，已核实）；`backend/skills/mx_core/execution.py:184-241`（`_handle_moni_trade` 无条件返回 `ok: True`）；`execution_runner_service.py:199-202`（以 `ok` 判定成功）
- 问题：`ERROR_HINTS`（`execution.py:10-33`）中 `"code=113"`、`"未绑定模拟组合账户"`、`"今日调用次数已达上限"` 等是**响应体**文本特征，而 httpx 异常消息只含状态行——这些 hint 在现有链路中结构性不可达，说明错误处理建立在对异常消息含响应体的错误假设上。若 MX API 以 200+错误体返回失败（配额耗尽、未绑定账户等），BUY/SELL 被记为 `completed`、`TradeOrder` 落库、run 状态 `completed`。
- 影响：交易台账数据损坏；测试 `test_service_guards.py:1918-1940` 用手工 raise 模拟异常消息，掩盖了真实链路产生不了这种消息的事实。

### B. 时间与时区

#### B1.【P1】run 日期过滤按 UTC 日界比较：凌晨 0–8 点的运行被归入前一天
- 位置：`aniu_service.py:551-557`（`list_runs`，已核实）；`db/models.py:146-148`（`started_at` 由 SQLite `CURRENT_TIMESTAMP` 生成 = naive UTC）
- 问题：`datetime.combine(run_date, datetime.min.time())` 用前端传入的**上海日历日**构造朴素边界，与 UTC 存储值直接比较，过滤窗口实际是上海 `[D 08:00, D+1 08:00)`。同文件 `_is_within_days`（860-863 行）对"今日"的判断却正确地先转上海时区，两处口径自相矛盾；`aniu_service.py:4762-4770` 处的另一查询正确做了 `astimezone(timezone.utc)` 转换。
- 影响：`/runs?date=` 与 `/runs-feed?date=` 的日期筛选对上海时间 00:00–08:00 开始的运行（夜间任务、凌晨重试常见）漏记/错记；与前端"今天"分组（浏览器本地日界）口径也不一致。

#### B2.【P1】CLS 快讯条目时间用服务器本地时区渲染：UTC 容器部署下全部偏移 8 小时
- 位置：`backend/app/services/cls_news_service.py:247`（已核实：`datetime.fromtimestamp(ctime_int, tz=datetime.now().astimezone().tzinfo)`）
- 问题：查询窗口（43-45 行）用调用方传入的上海时区，条目时间渲染却用**服务器本地时区**。全代码库其余部分都显式锚定 Asia/Shanghai，唯独此处。实测（TZ=UTC）：epoch 1760000000 渲染为 `08:53:20`，上海时间实为 `16:53:20`。
- 影响：Docker（默认 UTC）部署时，CLS 每条电报的 `time` 字段偏移 8 小时，直接进入 LLM 上下文——模型把"09:30 开盘时段电报"理解成"凌晨 01:30"，时间归因与盘中/盘后判断系统性错误。修复为 `SHANGHAI_TZ` 即可。

### C. 上下文与压缩

#### C1.【P1】持久会话压缩摘要从未注入提示词：压缩实际等于"删除上下文"
- 位置：`aniu_service.py:5358-5369`（`_build_persistent_session_context_system_message` 全后端无调用点，已核实）；`aniu_service.py:5345-5356`（`_build_persistent_session_prompt_messages` 中 `del session`——显式丢弃含 `archived_summary` 的 session）；`aniu_service.py:5290-5291`（历史用 `last_compacted_message_id` 挡掉旧消息）
- 问题：压缩触发后，被归档的消息从历史中移除，持久化的摘要消息又因 `message_kind == "live_turn"` 过滤不可见，`archived_summary` 只在 API 展示（743 行）被读取——**摘要只写库不进提示词**。另外 `_build_compacted_summary_text` 只摘要候选中最后 6 条 assistant 记录（5379 行），更早的连摘要都没有；`_merge_compacted_summary` 每次追加 `[增量归档]`，摘要文本无限增长。
- 影响：一旦触发压缩（未归档记录 >24 或 token 超预算），模型永久丢失被归档的全部上下文——"上下文压缩"功能整体失效为数据删除。修复接近一行级：在构建 prompt messages 前插入摘要 system 消息。

#### C2.【P1】用户聊天会话历史无任何裁剪/压缩：长会话必然溢出且不可恢复
- 位置：`backend/app/services/chat_session_service.py:713-722`（已核实：查询全部消息，无 limit）、`:608-652`
- 问题：`stream_chat` 每轮查询该会话**全部**消息全量转换为上下文，无条数上限、无 token 估算、无摘要压缩。`ChatSession` 模型专门设计了压缩字段（`archived_summary`/`last_compacted_message_id`），但仅自动化路径使用；`kind="user"` 的用户会话路径完全未接入。且每轮助手消息的 `meta_payload.replay_messages` 保存完整工具调用转录，历史重放时全量注入。
- 影响：会话轮数增多后上下文单调膨胀，最终超出模型窗口 → 400 → 触发非流式回退（同样 400）→ 本轮失败；由于历史只增不减，**该会话此后每一轮都失败**，用户只能删除会话。token 成本同时线性增长。

#### C3.【P1】历史附件每轮重新读盘、重新解析、图片重新 base64 内嵌
- 位置：`chat_session_service.py:616-625`、`:323-424`
- 问题：每轮对**每一条历史** user 消息重建附件内容：图片重新 base64（单图上限 6MB → 约 8MB 字符串）、docx/xlsx/pptx 每轮重新解压解析。文本预算 `ATTACHMENT_TOTAL_TEXT_LIMIT=24000` 是单条消息预算而非单次请求预算。
- 影响：与 C2 叠加后成本和延迟随历史规模二次膨胀；曾经发过的每张图片在之后每一轮都重复上传给模型。

#### C4.【P1】流式内容聚合混用"覆盖/追加"语义：失败/中断时落库内容拼接错误
- 位置：`chat_session_service.py:837-848`（`final_delta` 追加 vs `llm_message`/`final_finished`/`completed` 覆盖）；`llm_service.py:1108-1125`（`stream_mode` 由第一个 delta 定型）、`:897-912`（中途重试从头重发全部 delta）
- 问题：系统提示词明确要求模型"先说明拟执行操作再调用工具"（文本+工具调用同轮是设计预期），中间迭代的说明文本会先把 `final_content` 覆盖，最终迭代的 delta 又追加其上——成功路径仅靠最后的 `completed` 整体覆盖才正确；一旦中途失败或客户端断开，落库内容就是"中间说明文本 + 半截最终回答"的无分隔拼接。若文本 delta 先于 tool_call delta 到达，中间说明文本还会被当作最终答案流式播出。流式重试会向客户端重发全部 delta（重复内容）。
- 影响：失败/中断轮次保存的助手消息内容损坏；多轮工具调用场景下前端语义错乱。

### D. 启动与生命周期

#### D1.【P1】单个损坏技能可使整个后端无法启动：loader 缺乏故障隔离
- 位置：`backend/app/skills/loader.py:215`（`instance = cls()` 无 try/except，已核实）、`:182-185`（只捕获 ImportError）、`:232-235`（只捕获 OSError，**UnicodeDecodeError 是 ValueError 子类**会逃逸）、`:238`（frontmatter `metadata` 值为字符串时抛 AttributeError）；传播终点 `main.py:33`
- 问题：导入流程是"先落盘、后重载"（`skill_admin_service.py:476-489`），导入一个 SKILL.md 编码错误/frontmatter 非法/handler `__init__` 抛错的技能后，技能目录已永久落盘，此后**每次重启** `skill_registry.reload()` 都抛异常 → 应用无法启动，只能手工删除 workspace 目录。
- 影响：一个坏技能 = 整个平台永久无法启动。修复：`_scan_dir` 内对每个技能整体 try/except。

#### D2.【P1】交易日历远程接口是启动与调度的单点强依赖：无降级、无失败缓存
- 位置：`main.py:37`（`warm_up_months` 无 try/except）；`trading_calendar_service.py:87-102`（重试无退避，单次失败阻塞约 120 秒）、`:209-224`（缺月即 raise）、`:241-261`
- 问题：① 当前月份不在缓存且远端不可用时（镜像内置缓存只到 2026-12），应用**启动直接失败**——而运行期同样的失败被调度循环捕获容忍，行为不一致；② 运行期月份边界处 `is_trading_day` 抛异常会让整个 `process_due_schedule` 轮次中止，**所有定时任务停摆**；③ `_fetch_month` 4 次重试无退避间隔；④ 预热"下月"只在进程恰好在当月最后一个交易日启动时生效（`warm_up_months` 全仓库唯一调用点是 lifespan），长驻进程从不预热下月。
- 影响：月份切换 + 远端故障（或 CIIS 未发布新月数据）的组合下，调度系统完全停摆或服务无法启动。

### E. 数据统计与契约

#### E1.【P1】runtime overview 的"今日/近3天/近7天"统计仅基于最近 100 条 run
- 位置：`aniu_service.py:599-613`
- 问题：先 `list_runs(db, limit=100)` 再在 Python 侧按时间窗过滤。默认调度 `*/30 * * * *` 交易日全天约 48 条/天：3 个交易日 ≈ 144 条 > 100。
- 影响：默认配置下运行约 2 天后"近3天"统计即被截断，"近7天"只统计到约 40%（分析次数、成功率、API 调用数、交易数、token 用量全部偏低）。

#### E2.【P1】PUT /settings 前端 payload 缺失 5 个 automation_* 字段：后端全量覆盖导致配置被静默重置
- 位置：前端 `types.ts:1-15`（已核实：AppSettings 只有 `automation_context_window_tokens`）、`stores/legacy.ts:341-352`；后端 `schemas/aniu.py:19-34`、`aniu_service.py:463-481`（已核实：遍历 `model_dump()` 全字段 setattr）
- 问题：Pydantic `model_dump()` 会为前端未提交的字段填入 schema 默认值（如 `automation_session_id=None`、`automation_recent_message_limit=24`）并全部写回数据库。`automation_session_id` 有 slug 兜底自愈，其余 4 个调优参数被重置且无兜底。
- 影响：任何通过 API 自定义过这些参数的部署会在一次 UI 保存后静默丢失配置。

#### E3.【P1】事件流 `stream()` 无终止保障：终端事件可被积压丢弃，SSE 与前端永久挂起
- 位置：`event_bus.py:72-87`（已核实：积压超 2000 直接丢事件，不区分事件类型——`completed`/`failed` 同样可被丢弃）、`:127-149`（无限心跳循环，无 DB 状态兜底、无最大时长）
- 问题：`llm_service` 对最终答案逐分片发 `final_delta`（长答案事件量轻松达数千条），慢客户端触发 2000 上限现实可达。终端事件被丢后 `stream()` 退化为每 15 秒一条心跳的无限循环；订阅不存在/已过期的 run_id 同样进入永久心跳。前端 `useRunStream.ts:536-551` 读循环无超时，UI 永久显示"运行中"。
- 影响：SSE 连接与后端 worker 线程永久挂起；每次发生泄漏一个线程+一个通道。

### F. 前端状态管理

#### F1.【P1】聊天流式期间切换会话：流式内容写入错误会话、错误回滚误删其他会话消息
- 位置：`frontend/src/composables/useChatSession.ts:15-21`（已核实：`getStreamingAssistant` 取当前数组最后一条消息）、`:243`、`:293`；`ChatView.vue:142-152`
- 问题：流式事件回调通过"当前 messages 数组的最后一条 assistant 消息"定位追加目标，而非发起请求时的那条。`sendMessage` 期间无任何机制阻止会话切换——切换后 `final_started` 会把**新会话最后一条 AI 回复清空**，`final_delta` 把旧会话的流式内容追加进去；流抛错时回滚 `slice(0, -2)` 删除的是**新会话**的最后两条消息。
- 影响：等待 AI 回复（通常数十秒）时切换会话是高频操作，目标会话被污染/误删。

#### F2.【P1】手动运行完成后"今日"卡片永久钉在实时快照视图：完整详情与删除入口不可达
- 位置：`TasksView.vue:709-715`（已核实）、`useRunStream.ts:597`（`liveVisible = liveFocused && state.status !== 'idle'`）
- 问题：运行完成后 `state.status` 停在 `'completed'`（非 `'idle'`），`liveRunId` 无人复位（`reset()` 全项目无调用方），点击"今日"卡片永远走 `focusLiveCard()`。详情面板钉在流式快照上：执行概览/动作时间线/新闻源诊断/一致性修正双栏（`TasksView.vue:808-840` 均有 `!liveVisible` 前置）以及"删除任务"按钮（121 行）全部不可见。
- 影响：每次手动运行结束后的主要查看路径只能看到流式快照；完整分析详情需绕道"历史"区、整页刷新或等下一次运行覆盖。

---

## 三、P2 级发现（按子系统分组）

### 调度与执行

| # | 标题 | 位置 | 摘要 |
|---|---|---|---|
| 1 | 进程重启后遗留 "running" 状态的 run 无恢复机制 | `aniu_service.py:1941-1948`、`scheduler_service.py:24-27`、`main.py:30-42` | run 创建即 `status="running"`；shutdown 仅 `join(timeout=2)`，daemon 线程被硬杀；全库无启动时 stale-run 恢复逻辑；`delete_run` 对 running 默认拒绝删除（757-761 行）→ 永久"进行中"幽灵任务，并触发 E3 的永久心跳挂起 |
| 2 | 非法/永不匹配的 cron 静默失效 | `schemas/aniu.py:96`（仅校验长度）、`aniu_service.py:5528-5554` | 拼错的 cron 保存成功、任务显示"已启用"但永不触发，前后端均无提示；`_compute_next_run_at` 对永不匹配表达式做约 105 万次分钟级空转（5565 行），且 `list_schedules`/每轮轮询都重算 |
| 3 | 合法 cron 的星期区间跨 7 被误拒 | `aniu_service.py:5621-5634`、`5649-5661` | DOW `"6-7"`（周六日）、`"1-7"`（每天）被 ValueError 拒绝；`"0-7"` 退化为只剩周日。标准 cron 合法表达式被当作无效 |
| 4 | 调度失败退避可被 next_run_at 到期绕过 | `aniu_service.py:3091-3106` | `retry_after_at` 未到期时不 continue，直接落入 `next_run_at` 判断；cron 周期 ≤ 5 分钟时 5 分钟退避形同虚设 |
| 5 | `execute_plan` 无状态级防重/幂等保护 | `execution_runner_service.py:149-188` | 动作选择查询无 status 过滤；现状不重复仅是巧合（单调用点+全局锁）；未来任何重放入口都会把已 completed 的 BUY/SELL 再次真实下单 |
| 6 | 日历拉取失败可把已成功的 run 翻转为 failed，失败处理器二次抛出卡死 run | `aniu_service.py:2801-2805`、`2969-2988` | 成功路径内调用 `_compute_next_run_at` 触发远程日历拉取可抛异常 → run 被标 failed；失败处理器再次调用同样可抛 → run 卡在 running、调度反复重触发 |
| 7 | 交易日历月份一旦缓存永不刷新 | `trading_calendar_service.py:209-211` | 节假日修订/临时休市（如哀悼日休市）永远进不来；缓存无 TTL/版本比对，错误一旦固化无法自愈 |
| 8 | Jin10 分页游标依赖 createdAt 为纯数字，异常时静默截断 | `jin10_news_service.py:223-228, 248-264` | 上游字段格式变化时只取第一页，`has_more` 仅写入无人消费的 meta；上下文文本还宣称"已全量扫描 N 条" |
| 9 | CLS 分页无游标循环保护 | `cls_news_service.py:216-226` | 上游 bug 返回相同 nextCursor 时无限请求（Jin10 有防打转，CLS 没有）；两服务均无最大页数上限 |
| 10 | 账户总览回退快照可混合不同 run（不同时间点）的数据 | `aniu_service.py:1441-1445`、`3126-3160` | balance/positions/orders 各自从不同 run 取"第一个成功结果"拼成一个总览，总资产与持仓不对应同一时刻，盈亏计算失真 |

### 聊天与 LLM

| # | 标题 | 位置 | 摘要 |
|---|---|---|---|
| 11 | 删除会话不清理附件 | `chat_session_service.py:491-495` | `ChatAttachment` 无 session 外键，附件行与文件永不删除；A 会话附件可被引用到 B 会话消息 |
| 12 | 同一会话并发流式请求无互斥 | `chat_session_service.py:679-898` | 双方历史互不含对方消息，各自独立回答，消息交错落库 |
| 13 | 流式解析 `json.loads` 无异常保护 | `llm_service.py:1058` | 畸形数据块抛 JSONDecodeError，绕过全部重试逻辑直接失败 |
| 14 | 客户端断开后已执行的工具副作用（含真实交易）不写入历史 | `chat_session_service.py:861-898`、`llm_service.py:812-849` | 交易已成交但聊天历史无记录，下一轮模型不知道该笔交易发生过 |
| 15 | usage 统计：聊天路径完全丢弃；run 路径只记最后一轮 | `llm_service.py:1079-1081`、`aniu_service.py:1246-1253` | 多轮 agent 的累计 token 消耗被系统性少记 |
| 16 | token 估算对中文低估约 2.5-4 倍，图片场景又大幅高估 | `token_estimator.py:18-37` | 压缩触发过晚；`_normalize_content_length` 对 base64 图片用 json 长度高估 |
| 17 | `chat_get_run_detail` 将未截断的 api_details/trade_details/trade_orders 全量注入上下文 | `skills/chat_context/handler.py:331-335` | 同文件其他字段都截断，唯独这三个列表原样返回，一次调用即可撑爆上下文 |
| 18 | chat 与 chat_stream 的 LLM 超时不一致（1800s vs 180s） | `aniu_service.py:1581` vs `1639` | 慢模型下流式对话可用性显著低于非流式 |
| 19 | router 对 stream_chat 的异常映射是死代码 | `router.py:605-612` | 生成器函数的校验异常实际转为 SSE failed 事件，404/409 状态码永不出现 |
| 20 | `_run_body` 在校验 MX API Key 之前执行新闻预取 | `aniu_service.py:2563-2570` | 未配置 Key 的失败 run 白耗全部新闻诊断 LLM 调用 |

### 一致性校验（补充 A 组）

| # | 标题 | 位置 | 摘要 |
|---|---|---|---|
| 21 | 条件词过滤含单字"如"，"如下"句式造成漏检 | `aniu_service.py:3604、3626` | "执行情况如下：买入…"整行被跳过，声称与实际不一致被放过 |
| 22 | CANCEL 声称/实际比对系统性误判 | `aniu_service.py:3619、3849-3890` | claimed 目标取"委托"等普通名词，与实际撤单记录的股票名永不相交 → 已执行撤单仍被判缺口，追加与事实相反的警告 |
| 23 | 空闲压缩分支永不触发 | `aniu_service.py:5458-5465` | 压缩只在 run 收尾调用，此时 `last_message_at` 刚被更新为 now，`>= 12h` 不可能成立；`automation_idle_summary_hours` 配置形同虚设 |
| 24 | gap 判定与 autofill 使用两套提取器 | `aniu_service.py:3617-3618` vs `3660-3678` | "买入600519 1000股"（纯代码）能被 executable claims 提取但 gap 判定提取不到 → 不触发 autofill |

### 技能系统

| # | 标题 | 位置 | 摘要 |
|---|---|---|---|
| 25 | build_tools 与 execute_tool 的技能优先级不一致 | `skill_stack_service.py:224-230` vs `:249`；`registry.py:188-191` vs `:210` | 同名工具时 LLM 看到的 schema 来自技能 A，实际执行技能 B 的 handler |
| 26 | `edit_file` 允许空 old_text 破坏文件 | `builtin_utils/handler.py:728-748` | `replace_all=true` 时 `content.replace("", new)` 在每个字符间插入内容，整文件被破坏 |
| 27 | grep files_with_matches 模式计数缺陷 | `builtin_utils/handler.py:953-970` | `total_matches` 只计每文件 1 条；`counts[...] or ...` 是死代码 |
| 28 | `_resolve_query` 缺参数时回退用整个 task_prompt 作查询词 | `mx_core/execution.py:276-283` | 查询语义失控，且掩盖模型漏传参数的错误 |

### 数据层与配置

| # | 标题 | 位置 | 摘要 |
|---|---|---|---|
| 29 | `_ensure_app_settings_columns` 的数据回填被"缺列"条件门控 | `db/database.py:117-137` | 旧库值迁移（65536→131072 等）只在本次启动恰好有列缺失时执行；对比 `_ensure_chat_session_columns` 是无条件回填，模式不一致 |
| 30 | `_backfill_strategy_run_types` 每次启动重写全部历史 run | `db/database.py:355-451` | "一次性迁移"实际每次启动无条件运行；带 schedule_id 的历史记录被当前调度值覆盖（调度修改后历史被改写）；全表 JSON 读入内存逐行 UPDATE，启动耗时线性上升 |
| 31 | SQLite 并发写入未做 WAL / busy_timeout 配置 | `db/database.py:22-25` | 调度线程+运行 worker+API 线程池并存，默认 DELETE journal + 5 秒 busy timeout，并发碰撞时可能 "database is locked" |
| 32 | 运行时数据目录三处分裂 | `config.py:26-28,116-127`、`trading_calendar_service.py:31-33`、Dockerfile | DB 在 `backend/data/`、密钥在根 `data/`、日历缓存在 `backend/app/data/`（模块相对路径，Docker 中不随卷持久化） |

### 前端

| # | 标题 | 位置 | 摘要 |
|---|---|---|---|
| 33 | ScheduleView 保存静默删除所有非标准命名的定时任务 | `useScheduleForm.ts:215-262`（已核实）、`api.ts:281-286` | buildPayload 只构造 14 个固定模板任务，PUT 全量替换；任何命名不匹配的后端任务（旧版数据、手工创建）保存后被静默删除 |
| 34 | 交易时段任务"部分启用"往返后被整体启用/禁用 | `useScheduleForm.ts:209, 257` | 回填 `enabled = matched.some(...)`，保存统一应用，静默改变实际调度行为 |
| 35 | loadSession/loadOlderMessages/loadHistoryRuns 无竞态防护 | `useChatSession.ts:59-108`、`useAnalysisRuns.ts:1119-1143` | 慢响应覆盖新会话/新日期结果；分页把别的会话的历史消息拼进当前会话 |
| 36 | 聊天流无 AbortController，回滚与后端持久化状态分叉 | `useChatSession.ts:198-255, 291-296` | 流中断时本地删除服务器已保存的消息；离开页面后请求无法取消 |
| 37 | SSE 连接错误路径不设置 pendingPostRunId + runDetailsMap 无失效 | `useRunStream.ts:554-563`、`legacy.ts:243-253` | 流断开后重进页面，详情面板长时间停留在"进行中"旧缓存 |
| 38 | 登出不清 runStream 单例 | `App.vue:80-87`、`useRunStream.ts:712-721` | SSE 连接、liveFocused、sessionStorage 状态跨登录会话残留 |
| 39 | 流式接口非 2xx 时前端丢弃后端 detail | `useChatSession.ts:224-226`、`useRunStream.ts:529-531` | 用户看到"聊天流连接失败 (404)"而非"会话不存在"；与 `buildErrorMessage` 两套口径 |
| 40 | formatters.ts 对 NaN 无防护 | `formatters.ts:76-84, 156-160` | `formatMoney(NaN)` → "¥NaN"；同文件 `pnlClass` 却有 NaN 防护 |

---

## 四、P3 级发现（摘要）

- **死代码**：`RateLimiter.reset()`、`event_bus.is_finished()`（本可用于兜底判断）、`useRunStream.reset()/stop()/onEvent/heartbeatAt`、`next_trading_day`（测试还 monkeypatch 它制造虚假覆盖信号）、`_get_run_tool_calls` 读取永不存在的 `prefetched_tool_calls` 键、`parsers.py` 三函数、`_exec_utils.py` 整个模块、前端 `api.chat/chatStreamUrl`、`useAnalysisRuns` 的 `allRuns/shouldIncludeRun/filterVisibleRuns`。
- **平行实现漂移**：`registry.py` 与 `skill_stack_service.py` 逐行重复且已分化——**生产用后者，测试测前者**；`SkillContextProvider` 是死代码；`execution_plan_service.py:122-153` 复制了 `execution.py` 的交易校验逻辑需双处同步。
- **cron 细节**：2 年扫描上限错过 4 年一遇的 2 月 29 日合法 cron；`process_due_schedule` 在市场日不匹配时每 5 秒重算一遍 next_run_at。
- **展示细节**：`_order_status_text` 部成后撤单显示"部分成交"（撤单信息丢失）；`_build_trade_summaries` 排序键缺陷（缺失时间戳排最前、order_id 字符串排序）；`_build_account_overview` 把"无数据"显示为"0 盈亏"；`formatMoney(-0.001)` 显示 "¥-0.00"；`activeScheduleCards` 对 `*/30` 类 cron 显示 "00:00"。
- **其他**：`_normalize_reasoning_effort` 把 low/medium 全部映射为 high；工具参数为已解析 dict 时抛未捕获 TypeError；`except TypeError` 签名探测回退掩盖执行器内部错误；无 tool_call_id 的同名并发调用在聚合时丢失；`main.py:36` 预热用 `date.today()`（服务器本地日期）；CLS `important` 字符串 "false" 被判真；CLS 重试无退避间隔；掩码回写用 `"****"` 子串判断（真实密钥含该子串时被静默丢弃）；`delete_skill` 先提交 DB 再删目录；路由无 catch-all；`todayTradeCount` 用浏览器本地时区；SettingsView 技能操作 unhandled rejection；新建会话按钮 500ms 时间型防抖而非请求型守卫；`saveSchedule` 保存后双重请求；`router.py` 使用 `Literal` 未导入（依赖 FastAPI 宽松求值才未崩溃，升级脆弱）；models 与 schema 字段长度约束不一致（255 vs 512）+ 多个死列。

---

## 五、各子系统健康度总评

| 子系统 | 健康度 | 核心问题 |
|---|---|---|
| 后端核心层（main/core/db） | 中偏上 | 生命周期顺序、UTC 存储约定、会话管理自洽；但迁移逻辑留在启动路径、SQLite 并发未调优 |
| aniu_service（5911 行主服务） | 中 | 展示/提取逻辑防御性强；一致性 enforcement 子系统存在成体系缺陷（A1-A4），时间口径只贯彻了 90% |
| 聊天与 LLM 服务 | 中偏下 | 架构设计（replay_messages、失败落库语义）有想法；但上下文生命周期断裂（C2-C4）使长会话必然走向不可恢复 |
| 执行与调度链路 | 中 | 时区处理正确且有测试锁定；但可靠性完全依赖"单进程内一切正常"假设，无恢复路径 |
| 技能系统 | 中偏上 | 分层合理、启停持久化正确；loader 是无故障隔离的全系统单点（D1） |
| 数据源服务 | 中偏上 | 类型防御严格、失败优雅降级；CLS 时区渲染（B2）与日历单点依赖（D2）是两个真实破口 |
| API 契约 | 良好（约 90%） | 37 条路由与前端逐一对齐、分页/SSE 契约自洽；两个 P1 均属"语义正确、边界错误"型 |
| 前端 | 中 | 展示层细节用心（reveal 定时器、markdown 节流）；异步竞态防护与状态生命周期归属是系统性短板 |

---

## 六、修复优先级建议

**第一优先（交易正确性，1-2 天工作量）：**
1. `_enforce_trade_consistency` 增加 `run_type == "trade"` 门控（对照自选股版本，几行代码）——A1
2. `MXClient._post` 解析响应体错误码/消息，统一抛带上下文的异常——A5
3. followup 调用传入 `_planning_tool_executor`——A3
4. 统一 gap 判定与 autofill 的比对粒度（名称级判重或五元组判 gap）——A2

**第二优先（数据正确性，各为一行级/局部改动）：**
5. `list_runs` 日期过滤改为上海日界换算成 UTC 区间（同文件 4762 行已有正确写法可抄）——B1
6. CLS 条目时间改用 `SHANGHAI_TZ`——B2
7. 压缩摘要注入提示词（在 `_build_persistent_session_prompt_messages` 前插 system 摘要消息）——C1
8. runtime overview 改为按时间窗口聚合 SQL 查询——E1

**第三优先（可用性/健壮性）：**
9. `_scan_dir` 对每个技能整体 try/except——D1
10. 用户聊天会话接入与自动化路径同源的 recent_limit/压缩机制——C2
11. 启动时将遗留 running run 标记失败 + `stream()` 增加 DB 状态兜底或最大时长——E3
12. `stream_chat` 统一 final_content 语义（仅 completed/final_finished 覆盖）——C4
13. 前端聊天流式定位改为闭包捕获目标消息 + 会话切换守卫——F1
14. 运行终态后复位 liveRunId/liveFocused——F2
15. ScheduleView 保存前对将被删除的非模板任务加确认提示——P2-33

**第四优先（中期治理）：**
16. 为启动路径的迁移逻辑加幂等标记（schema version 表）
17. 日历增加失败负缓存与退避、月份 TTL
18. 统一前端错误处理到 `buildErrorMessage`/`handleUnauthorized`
19. 收敛 registry/skill_stack_service 平行实现（测试改测生产路径）
20. SQLite 开启 WAL + busy_timeout

---

*报告结束。所有 P1 级发现均经审计代理交叉验证，其中 A1/A5/B1/B2/C1/C2/D1/E2/E3/F1/F2 及关键链条由审计者亲自读代码复核确认。*
