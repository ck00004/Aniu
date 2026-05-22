# Aniu-main 业务逻辑说明书

这份文档专门服务于二次开发中的业务逻辑修改。

它回答的问题不是“代码在哪”，而是：

- 业务对象有哪些。
- 每条业务链路是怎么流转的。
- 状态如何变化。
- 失败时怎么兜底。
- 想改业务规则时，真正应该改哪一段逻辑。

建议将这份文档和 DEVELOPMENT_HANDBOOK.md 配合使用：

- DEVELOPMENT_HANDBOOK.md 负责代码结构与改动定位。
- 本文负责业务语义、状态流转、规则与修改影响面。

---

## 1. 业务总览

Aniu-main 当前的核心业务可以拆成七个子系统：

1. 认证与单用户访问控制
2. 应用设置与运行参数管理
3. 账户数据聚合与总览展示
4. 分析 / 交易任务执行
5. 定时调度与失败重试
6. AI 聊天与附件会话
7. 技能系统与工具调用运行时

这七个子系统中，真正驱动项目价值的是三条主链：

- 任务运行链
- 自动化会话链
- 账户数据链

这三个链路决定了系统是否能持续分析、持续记忆、持续形成交易动作。

---

## 2. 核心业务对象

### 2.1 AppSettings

业务含义：

- 系统的唯一全局配置对象
- 保存大模型配置、妙想密钥、系统提示词、自动化上下文参数、禁用技能集合

业务特点：

- 系统默认只维护一条设置记录
- 如果数据库里没有，会在首次访问时自动创建
- 敏感字段在读取时会掩码，但保存时支持保留原值

二次开发时常见修改方向：

- 增加新的模型参数
- 增加新的自动化策略参数
- 增加新的第三方服务配置

高风险点：

- 新字段不仅要改表，还要改默认创建逻辑和前端设置页

### 2.2 StrategySchedule

业务含义：

- 一条定时任务配置
- 描述任务什么时候触发、执行什么类型、用什么提示词、超时时间是多少

关键字段语义：

- run_type: analysis 或 trade
- cron_expression: 运行计划
- task_prompt: 本轮任务提示词
- enabled: 是否启用
- retry_count: 当前失败重试计数
- retry_after_at: 延迟重试时间点
- next_run_at: 下次计划执行时间

业务特点：

- 启用后必须始终能推导出 next_run_at
- 失败后会进入有限次数的延迟重试
- 非交易日不会执行，会顺延到下一个交易日

### 2.3 StrategyRun

业务含义：

- 一次实际运行的任务实例
- 不论手动还是定时，只要发生一次任务执行，就会产生一条运行记录

关键字段语义：

- trigger_source: manual 或 schedule
- run_type: 本轮实际执行类型
- status: running / completed / failed
- analysis_summary: 用于列表展示的简短摘要
- final_answer: 本轮最终自然语言结论
- llm_request_payload / llm_response_payload: 模型交互原始载荷
- skill_payloads: 工具调用链与运行轨迹
- executed_actions: 抽取出的交易动作
- chat_session_id / prompt_message_id / response_message_id: 关联自动化会话上下文

业务特点：

- 它是任务审计中心
- 也是账户快照的兜底数据源之一

### 2.4 TradeOrder

业务含义：

- 从一次任务中提炼并持久化出来的模拟交易动作

业务特点：

- 不是所有运行都会产生交易记录
- 只有 executed_actions 中的 BUY / SELL 会落到这里
- 它和 run 是一对多关系

### 2.5 ChatSession / ChatMessageRecord

系统里其实有两种会话：

- 用户聊天会话，kind = user
- 自动化运行会话，kind = automation

用户会话用于聊天页。
自动化会话用于把历次任务串成持续上下文。

这两个概念必须区分，否则很容易把“聊天历史”和“自动化策略记忆”混在一起。

### 2.6 ChatAttachment

业务含义：

- 聊天上传文件的元数据

业务特点：

- 文件内容落在运行目录的 chat_uploads 下
- 数据库存储的是元信息和存储路径
- 附件支持文本抽取与图像引用，但有类型和大小限制

---

## 3. 认证与访问控制业务

### 3.1 登录模型

Aniu-main 当前是单用户模型，而不是多租户用户系统。

业务规则：

1. 登录密码来自环境变量 APP_LOGIN_PASSWORD
2. 未配置密码时，系统直接拒绝登录
3. 登录成功后签发 JWT
4. 后续所有受保护接口依赖 Bearer Token

业务含义：

- 这是一个“单实例、单操作者”的控制模型
- 当前没有用户表、角色表、权限表

如果后续要改成多用户，这不是补几个字段的问题，而是整个业务模型要重做。

### 3.2 限流规则

限流使用内存滑动窗口，按“客户端 IP + 路由族”计算。

当前重点限流对象：

- 登录
- 手动运行
- 聊天
- 上传附件
- 技能导入

业务含义：

- 限流的目标是保护单实例后端，避免被瞬时请求打穿
- 由于是内存限流，重启后不会保留状态

修改建议：

- 如果部署到多实例，当前限流就不再可靠，需要换成共享存储实现

---

## 4. 设置管理业务

### 4.1 设置初始化规则

首次读取设置时，如果 app_settings 表为空：

1. 从环境变量读取默认值
2. 创建唯一设置记录
3. 写入默认系统提示词

业务含义：

- 环境变量是首次默认值来源，不是唯一事实来源
- 进入系统后，数据库设置成为运行时主要事实来源

### 4.2 设置保存规则

保存设置时：

1. 对所有字段逐个覆盖
2. 对 mx_api_key 和 llm_api_key 这类掩码字段，如果值里包含 ****，则视为“前端未修改”，不覆盖原值
3. 保存后立即刷新数据库对象

业务含义：

- 前端可以安全地回显掩码密钥
- 保存时不会误把掩码写回数据库

二次开发时常见误区：

- 前端以为设置读取的是环境变量，其实运行逻辑更偏向数据库快照
- 如果新增配置项，需要同时补全“创建默认值”“更新逻辑”“前端类型”和“设置页 UI”

---

## 5. 账户数据业务

### 5.1 账户总览的真实数据来源

账户总览并不是只依赖实时接口，它是一个分层兜底模型：

第一层：进程内缓存
第二层：最近运行记录中提取出的账户快照
第三层：实时调用妙想接口

具体流程：

1. 如果不是强制刷新，并且进程缓存没过期，直接返回缓存
2. 否则尝试从最近的 StrategyRun 中提取 balance / positions / orders 工具结果
3. 如果配置了 mx_api_key，则再尝试实时调妙想接口
4. 某个实时接口失败时，优先回退到最近运行快照
5. 如果实时和缓存都没有，就返回空账户总览并带错误信息

业务含义：

- 总览页不要求后端每次都实时拿到账户接口数据
- 即使妙想接口短暂失败，只要最近运行成功过，页面仍可展示“最近一次可信快照”

### 5.2 为什么账户总览会出现“旧数据”

这是系统设计，不是异常。

原因有三层：

1. 有进程内 TTL 缓存
2. 账户接口失败时允许回退到运行快照
3. 前端手动刷新还有冷却时间控制

如果要改成“必须实时”，需要同时改：

- 后端缓存策略
- 运行快照兜底逻辑
- 前端刷新冷却策略

### 5.3 账户总览里包含哪些业务加工

账户原始接口返回不会直接透传到前端，会被转换为：

- 总资产、现金、持仓市值
- 持仓列表
- 委托列表
- 已闭环交易摘要
- 错误信息列表

业务含义：

- 总览页展示的是“统一语义模型”
- 如果要加新的统计口径，最好在后端聚合层加，而不是在前端拼原始字段

---

## 6. 任务运行业务

这是系统最重要的业务链。

### 6.1 运行类型

当前只有两种任务类型：

- analysis: 分析任务
- trade: 交易任务

业务语义：

- analysis 更偏向分析、研判、总结
- trade 更偏向直接形成交易动作与执行记录

手动运行时，如果没有指定 schedule：

- analysis 使用手动分析提示词
- trade 使用手动交易提示词

如果绑定了 schedule：

- 实际 run_type 以 schedule.run_type 为准
- task_prompt 也优先使用该 schedule 的 task_prompt

### 6.2 一次运行的主流程

一次运行的完整业务流程如下：

1. 获取运行锁，防止并发执行多个任务
2. 创建 StrategyRun，状态先置为 running
3. 生成 settings_snapshot，冻结本轮执行参数
4. 校验 mx_api_key 是否存在
5. 创建 MXClient
6. 准备自动化会话上下文
7. 把上下文消息交给 llm_service.run_agent_with_messages
8. 获取 final_answer、tool_calls、runtime_trace
9. 从 tool_calls 中抽取 executed_actions
10. 将 BUY / SELL 持久化为 TradeOrder
11. 写回 StrategyRun 的各类 payload 和最终状态
12. 更新关联 schedule 的 last_run_at / next_run_at / retry 状态
13. 将本轮结果写入自动化会话
14. 必要时压缩自动化会话上下文

### 6.3 运行锁的业务意义

系统不允许同时存在两个进行中的任务。

业务目的：

- 防止多个任务同时操作同一套账户上下文
- 防止自动化会话记忆被并发写乱
- 防止多个任务抢占同一个妙想账户和工具运行时

这意味着：

- 如果你想支持并行多策略，这将是架构级改动，不是把锁删掉这么简单

### 6.4 settings_snapshot 的业务意义

运行开始时会把设置拍平为一个快照传入执行流程。

业务目的：

- 保证一轮运行中，配置保持一致
- 避免运行过程中用户改设置导致上下文漂移

这意味着：

- “保存设置后为什么本轮任务没立刻变化”是正常现象，新设置影响下一轮运行

### 6.5 任务执行后的结果沉淀

一轮任务完成后，结果会沉淀到四个地方：

1. StrategyRun
2. TradeOrder
3. 自动化会话中的 user / assistant 消息
4. 账户总览可复用的工具快照

业务含义：

- StrategyRun 是审计与回放的主来源
- 自动化会话是后续持续决策的记忆来源
- TradeOrder 是交易动作结构化存档
- 账户快照是账户页的兜底数据源

### 6.6 分析任务的完整行为拆解

这里说的“分析任务”包括两类入口：

- 定时任务中 run_type = analysis 的任务
- 手动运行时未显式指定 trade 的任务

#### 6.6.1 进入分析任务前，系统先冻结哪些输入

在 _prepare_run 阶段，系统会先生成一份 settings_snapshot，并把下面这些信息冻结到本轮运行中：

- 当前 run_type
- 当前日期是交易日还是非交易日
- 当前使用的 task_prompt
- 当前的大模型配置
- 当前的自动化上下文配置
- 当前的 Jin10 / CLS 预分析配置

业务含义：

- 分析任务一旦开始，本轮不会再受设置页中途修改影响
- 手动运行分析时，如果 settings.task_prompt 非空，会优先用它；否则退回手动分析默认 prompt

#### 6.6.2 分析任务的固定前置动作

分析任务在进入大模型前，并不会直接开始问答，而是先做两件事：

1. 资讯预分析
2. 自动化会话上下文拼装

资讯预分析行为：

- 如果配置了 Jin10 base_url，就先抓取当天 Jin10 新闻并做分块预分析
- 如果配置了 CLS base_url，就先抓取当天 CLS 电报并做分块预分析
- 两个来源的诊断结果会合并成 prefetched_context
- 同时保留 prefetched_context_meta，供运行记录和前端展示使用

自动化会话拼装行为：

- 读取共享 automation 会话
- 写入一条 user 消息，内容包括时间、触发来源、任务类型、本轮提示词、资讯摘要与运行引导
- 取最近未压缩的历史消息
- 必要时把 archived_summary 作为系统级摘要注入

业务含义：

- 分析任务不是“只看本轮 prompt”
- 它会同时看到长期自动化记忆和当天资讯预分析结果

#### 6.6.3 分析任务实际能看到哪些工具

analysis 模式下，模型能看到的工具集合来自 TOOL_PROFILES.analysis。

当前包括：

- mx_query_market
- mx_search_news
- mx_screen_stocks
- mx_get_positions
- mx_get_balance
- mx_get_orders
- mx_get_self_selects
- mx_manage_self_select

当前不包括：

- mx_moni_trade
- mx_moni_cancel

业务含义：

- 现有设计里，分析任务可以读账户、读行情、筛股、查新闻、管理自选
- 但分析任务本身默认不允许直接下单或撤单

#### 6.6.4 分析任务的模型决策阶段

llm_service.run_agent_with_messages 会驱动一个多轮 tool loop：

1. 给模型发送 messages + analysis 工具集
2. 模型返回普通文本或 tool_calls
3. 只读工具会立即执行并回填结果
4. 变更类工具不会立刻执行真实动作，而是先转成 PlannedActionDraft
5. 循环直到模型不再调用工具，输出 final_answer

在 analysis 模式下，真正可能进入“先计划、后执行”的变更工具只有：

- mx_manage_self_select

业务含义：

- 分析任务里，自选股变更属于受控执行，不是模型一说就直接改
- 模型看到的是“计划结果”，真实执行发生在 LLM 阶段之后

#### 6.6.5 分析任务的一致性修正阶段

分析任务结束首次 LLM 决策后，会进入两层一致性检查：

1. 自选股一致性检查
2. 交易一致性检查

但在现有工具边界下，两者作用不同：

- 自选股一致性检查是 analysis 的核心修正逻辑
- 交易一致性检查在 analysis 模式下通常为空操作，因为 analysis 模式默认看不到下单/撤单工具

自选股一致性检查会做三件事：

1. 检查 final_answer 是否声称做了自选股增删
2. 对照 tool_calls 中是否真的存在可解析的自选股动作
3. 如果声明了但没执行，则先提炼标准 JSON，再基于该 JSON 自动补齐工具计划；补不齐时再要求模型生成纠正文案

当前实现里，这一步不会直接把文本改成工具调用，而是先生成一份标准化的一致性分析对象：

- schema = consistency_operation_v1
- check_name = self_select
- claimed_changes: 从结论中提炼出的自选股增删声明
- existing_actions: 当前已经存在的 planned / executed 动作
- operations: 需要补齐的标准化操作列表
- materialized_operations / materialized_tool_calls: 基于 operations 回填出来的计划工具结果
- autofill_applied: 是否已成功生成补齐计划

这份结构会挂到：

- StrategyRun.decision_payload.consistency_analysis.self_select

业务含义：

- 分析任务里“口头说加入自选”并不算完成
- 系统要求自然语言结论和工具执行记录对齐
- 一致性修正的核心输入不再只是原始文本，而是“文本提取后的标准 JSON”

#### 6.6.6 分析任务的执行与落库

如果 analysis 任务形成了自选股计划动作：

- execution_runner_service 会按顺序执行这些计划
- 自选股这类非交易动作最多尝试 2 次
- 结果写入 StrategyRunAction / StrategyRunActionResult

最终 run 会落下这些结果：

- StrategyRun.final_answer
- StrategyRun.analysis_summary
- StrategyRun.skill_payloads
- StrategyRun.decision_payload.consistency_analysis
- StrategyRun.executed_actions
- 自动化会话 assistant 消息

analysis 任务通常不会生成 TradeOrder，除非你后续放开 analysis 模式下的交易工具。

#### 6.6.7 分析任务的行为结论

可以把当前 analysis 任务理解成：

- 一个带长期上下文和资讯预分析的多轮调研任务
- 它可以形成观点
- 它可以受控维护自选股
- 但默认不直接产生交易委托

### 6.7 交易任务的完整行为拆解

这里说的“交易任务”也包括两类入口：

- 定时任务中 run_type = trade 的任务
- 手动运行时显式指定 run_type = trade 的任务

#### 6.7.1 交易任务与分析任务共用哪些基础能力

交易任务和分析任务共用这些基础流程：

- settings_snapshot 冻结
- Jin10 / CLS 资讯预分析
- 自动化会话上下文拼装
- 同一套 llm_service 多轮工具调用框架
- 同一套运行记录、事件流和失败处理框架

业务含义：

- 交易任务不是完全独立的执行器
- 它是在“分析任务框架”上加了交易工具和交易执行语义

#### 6.7.2 交易任务实际能看到哪些工具

trade 模式下，模型能看到：

- analysis 模式全部公共工具
- mx_moni_trade
- mx_moni_cancel

这意味着 trade 模式既能继续查行情/资讯/账户，也能生成真实模拟交易动作。

业务含义：

- 交易任务不是“直接下单页”
- 它仍然是“先调研，再决定是否执行”的智能任务

#### 6.7.3 交易任务的典型决策顺序

当前源码没有把顺序写死，但典型路径是：

1. 先读账户资金、持仓、委托
2. 必要时看新闻、查行情、筛股票
3. 形成 BUY / SELL / CANCEL 计划
4. 由执行器顺序执行计划动作
5. 再汇总出最终自然语言结论

业务含义：

- 交易任务的本质不是一句“买某只股票”
- 而是“基于账户约束和外部信息，形成并执行一组交易动作”

#### 6.7.4 交易任务的一致性修正阶段

trade 路径和 analysis 路径最大的差异之一，是一致性修正逻辑的重点不同。

当前规则：

- self_select consistency 在 trade 模式下直接跳过修正，只保留动作抽取结果
- trade consistency 才是 trade 模式的核心修正逻辑

trade consistency 会检查：

1. final_answer 是否声称已经买入、卖出或撤单
2. tool_calls 中是否真的存在对应的交易动作
3. 如果声明了但没执行，能否从结论中解析出明确 action / code / quantity

修正策略按优先级分三层：

1. 能明确解析时，系统先生成标准 JSON，再根据该 JSON 自动补一条受限交易工具计划
2. 不能自动补时，要求模型再输出一轮纠正文案
3. 最终仍会把结论修饰为与执行记录一致

当前交易一致性阶段也会先生成一份标准化结构：

- schema = consistency_operation_v1
- check_name = trade
- claimed_changes: 从结论里识别出的买卖撤声明
- executable_claims: 可直接转成委托参数的交易声明
- existing_actions: 当前已有的 planned / executed 动作
- operations: 待补齐的交易操作列表
- operation_key: action + symbol + quantity + price_type + price 的去重键
- materialized_operations / materialized_tool_calls: 基于 operations 生成的计划工具结果
- autofill_applied: 是否已成功生成补齐计划

这份结构会挂到：

- StrategyRun.decision_payload.consistency_analysis.trade

业务含义：

- trade 模式要求“说过的交易动作尽量真实落地”
- 它不是只修文案，而是优先补执行计划
- 同一条交易声明会先经过标准 JSON 去重，再决定是否补计划，避免一致性阶段重复补单

#### 6.7.5 交易计划如何真正执行

交易任务的变更工具不会在 LLM 阶段直接落单，而是分两层：

第一层：execution_plan_service

- 校验 action / symbol / quantity / price_type / price / order_id
- 生成 PlannedActionDraft
- 不做真实下单，只生成计划

第二层：execution_runner_service

- 按 sequence_no 顺序执行计划
- BUY / SELL 计划动作强制单次提交
- MANAGE_SELF_SELECT、CANCEL 等非买卖动作仍可按默认最多 2 次尝试
- 每次尝试都写入 StrategyRunActionResult
- 最终把成功动作归一化成 executed_actions

业务含义：

- 模型负责“决定做什么”
- 执行器负责“按平台规则真正去做”
- 当前系统已经显式避免 BUY / SELL 自动重试导致的重复买卖风险

#### 6.7.6 哪些交易动作会真正写入 TradeOrder

当前只有 executed_actions 里的以下动作会写入 TradeOrder：

- BUY
- SELL

不会写入 TradeOrder 的包括：

- CANCEL
- MANAGE_SELF_SELECT

它们仍然会留在：

- StrategyRun.executed_actions
- StrategyRun.skill_payloads
- StrategyRunAction / StrategyRunActionResult

业务含义：

- TradeOrder 不是“所有执行动作总表”
- 它只是买卖委托的结构化存档

#### 6.7.7 为什么交易任务有时会有结论但仍然失败

trade 任务的失败不只由大模型调用是否成功决定。

只要出现下面任一情况，run 都可能被标成 failed：

- 计划动作未完全执行
- BUY / SELL 单次提交失败
- 非交易动作重试后仍失败
- execution_summary 里 unresolved_count 大于 0

此时系统仍可能同时存在：

- final_answer
- 部分 executed_actions
- 部分成功的工具调用
- decision_payload.consistency_analysis

业务含义：

- “有结论”不等于“执行完全成功”
- run.status 反映的是整轮业务闭环是否完整落地

### 6.8 分析与交易的行为差异矩阵

| 维度 | analysis | trade |
|------|------|------|
| 手动入口默认类型 | 默认 | 需显式指定 |
| 定时入口常见任务名 | 盘前分析、午间复盘、收盘分析、夜间分析 | 上午运行X号、下午运行X号 |
| 工具集 | 公共工具 + 自选股管理 | 公共工具 + 自选股管理 + 下单/撤单 |
| 允许的核心变更动作 | 自选股增删 | 买入、卖出、撤单、自选股增删 |
| 一致性修正重点 | 自选股声明是否真实执行 | 交易声明是否真实执行 |
| self_select consistency | 启用 | 基本跳过修正 |
| trade consistency | 通常为空操作 | 核心修正逻辑 |
| 一致性中间产物 | consistency_operation_v1 self_select JSON | consistency_operation_v1 trade JSON |
| 典型副作用 | 更新自动化记忆、可能更新自选股 | 更新自动化记忆、可能写入 TradeOrder |
| 是否默认写 TradeOrder | 否 | 是，但仅 BUY / SELL |
| 变更动作重试 | 自选股等非交易动作最多 2 次 | BUY / SELL 单次提交，非交易动作最多 2 次 |
| 最终失败常见原因 | LLM 失败、自选股执行失败 | LLM 失败、交易计划未完全落地 |

### 6.9 这套逻辑里最容易被误判的几个点

#### 6.9.1 交易任务也会先做资讯预分析

当前 _run_body 在 analysis 和 trade 两条路径上，都会先调用 _prefetch_analysis_context。

业务含义：

- trade 不是“只看账户然后下单”
- 它同样会引入当天 Jin10 / CLS 的预分析结果作为上下文

#### 6.9.2 分析和交易默认共享同一个自动化会话

当前 automation 会话是共享的，而不是按 run_type 隔离。

业务含义：

- 昨天的分析结论会进入今天的交易上下文
- 今天的交易失败信息也会进入下一轮分析上下文

如果后续要做逻辑隔离，首先要改的是自动化会话路由，而不是 prompt。

#### 6.9.3 模型调用变更工具，不等于动作已经执行

在当前设计里：

- mutation tool 在 LLM 阶段多数只是生成计划
- 一致性修正阶段生成的 materialized_tool_calls 也只是计划结果，不是最终成交结果
- 真正执行发生在 execution_runner_service

业务含义：

- 看见 tool_calls 里有 mx_moni_trade，不代表账户里一定已有对应委托
- 看见 decision_payload.consistency_analysis 里有 operations，也不代表这些动作已经执行成功
- 真正执行结果应以 executed_actions、StrategyRunActionResult 和 TradeOrder 为准

#### 6.9.4 run.status = failed 并不代表整轮没有产出

当前失败语义是“整轮闭环未完全落地”，不是“完全没有输出”。

因此 failed run 仍可能同时拥有：

- final_answer
- tool_calls
- partial executed_actions
- 自动化会话里的失败 assistant 消息

#### 6.9.5 取消委托不会进入 TradeOrder

这不是遗漏，而是当前的数据建模选择。

业务含义：

- 如果你想做“全部动作流水”，应该优先看 StrategyRunAction / StrategyRunActionResult
- 而不是扩展 TradeOrder 去承载所有动作类型

#### 6.9.6 “每条动作最多尝试 2 次”不再适用于买卖委托

当前这个说法只对非交易动作仍然成立。

现在 execution_runner_service 会按 action_type 区分：

- BUY / SELL: 单次提交
- 其他动作: 仍按默认上限尝试

业务含义：

- 这是为了避免上游在第一次已提交但返回不确定失败时，自动重试再次发出同一笔买卖委托
- 如果你要恢复 BUY / SELL 自动重试，必须同时设计幂等键或委托去重机制，否则风险很高

### 6.10 后续调整逻辑时，优先改哪一层

#### 想改“分析任务能不能直接下单”

优先看：

- backend/skills/mx_core/tool_specs.py 中的 TOOL_PROFILES.analysis
- backend/app/services/aniu_service.py 中的 analysis 一致性与执行阶段

#### 想改“交易任务是否必须先读账户再下单”

优先看：

- trade_execution_guidance prompt
- llm_service 的 tool loop
- 可能需要新增执行前校验，而不是只改提示词

#### 想改“分析和交易是否共享上下文”

优先看：

- _get_or_create_persistent_session
- _prepare_persistent_session_context
- automation_session_id 的维护方式

#### 想改“资讯预分析只用于分析、不用于交易”

优先看：

- _run_body 中 _prefetch_analysis_context 的调用位置
- prefetched_context 写入 user 消息的逻辑

#### 想改“交易失败的判定口径”

优先看：

- execution_reconcile_service
- execution_runner_service._summarize_actions
- build_run_error_message

#### 想改“一致性修正提取出来的标准 JSON”

优先看：

- aniu_service._merge_consistency_analysis
- aniu_service._materialize_consistency_operations
- aniu_service._build_self_select_consistency_autofill_tool_calls
- aniu_service._analyze_trade_consistency

#### 想改“BUY / SELL 是否允许自动重试”

优先看：

- execution_runner_service.execute_plan
- execution_runner_service._resolve_action_max_attempts
- 如果要恢复重试，必须先补交易幂等或重复委托识别逻辑

#### 想改“哪些动作要落到 TradeOrder”

优先看：

- _run_body 中 persisted_trade_orders 的构造逻辑
- TradeOrder 表的语义是否仍只表示买卖委托

---

## 7. 自动化会话业务

这是 Aniu-main 区别于单次问答系统的核心设计。

### 7.1 自动化会话是什么

系统维护一个特殊的 ChatSession：

- kind = automation
- slug = automation-default

它不是给用户聊天页使用的，而是给系统的分析 / 交易任务使用。

每次手动运行和定时运行，都会共享这个会话。

### 7.2 自动化会话的业务价值

它解决的问题是：

- 前一轮任务的结论，下一轮能继续看见
- 手动运行和定时运行能共享历史上下文
- 失败记录也会进入上下文，便于后续修正
- 长期运行时可以形成压缩摘要，而不是无限膨胀

### 7.3 每轮运行如何写入自动化会话

每轮任务都会向自动化会话写入两条消息：

1. user 消息：描述时间、来源、任务类型和本轮任务提示词
2. assistant 消息：描述本轮结果或失败原因

当前 user 消息内容包含：

- 时间
- 来源是手动还是定时
- 任务类型是分析还是交易
- 本轮任务提示词

当前 assistant 消息内容规则：

- 成功时写 final_answer
- 失败时写 执行失败：具体错误

业务含义：

- 自动化会话本质上是“策略运行日志的可读化版本”
- 如果你想让下一轮更好理解上一轮，可以优先改这两类消息的格式，而不是先改模型提示词

### 7.4 自动化会话的压缩逻辑

会话不会无限制增长，系统会在以下条件满足时尝试压缩：

1. 历史消息数超过 recent_message_limit
2. 估算 token 超过安全预算
3. 距离上次消息已经空闲超过 automation_idle_summary_hours

压缩后的结果写入：

- session.archived_summary
- session.summary_revision
- session.last_compacted_message_id
- session.last_compacted_run_id

业务含义：

- 压缩后，旧消息不会全部消失，而是被总结成摘要并作为系统上下文继续注入
- 这是一种“保留长期记忆，但缩短 prompt”的折中方案

### 7.5 自动化会话上下文如何参与下一轮运行

下一轮运行时，系统会构建一组 prompt messages，来源包括：

1. 已压缩的 archived_summary，作为系统消息
2. 未被压缩掉的近期真实历史消息
3. 未来预留的长期记忆注入接口

业务含义：

- 系统不是单纯把全部历史喂给模型
- 它是在“摘要 + 最近消息”的混合模式下工作

---

## 8. 定时调度业务

### 8.1 调度线程模型

调度系统由后台线程定期轮询。

轮询间隔来自配置：

- scheduler_poll_seconds

每次轮询会调用 process_due_schedule。

### 8.2 判定一个任务是否该执行

调度扫描逻辑会依次判断：

1. 任务是否启用
2. next_run_at 是否存在，不存在则立即补算
3. 今天是不是交易日，不是则顺延到下一交易日
4. 是否存在 retry_after_at，且已经到期
5. next_run_at 是否已经到点

如果多个任务同时到点，会优先选择最早应该触发的那个。

业务含义：

- 当前调度器不是并发批量执行模式，而是单任务优先模式

### 8.3 定时任务失败重试规则

只有 trigger_source = schedule 时，失败才会触发延迟重试逻辑。

当前规则：

- 最多重试 3 次
- 每次失败后延迟 5 分钟再试
- 超过最大次数后清空 retry_after_at，等待下一次正常计划触发

业务含义：

- 手动运行失败不会进入调度重试
- 定时任务的 retry 只是短期兜底，不是无限补偿

如果你要改成更复杂的补偿策略，需要同时改：

- retry_count 的语义
- retry_after_at 的计算方式
- 前端调度展示文本

### 8.4 非交易日顺延规则

cron 表达式并不是唯一触发依据。

系统还会额外判断：

- 当前日期是不是交易日

如果不是，会直接跳到下一个交易日重新计算。

业务含义：

- 当前调度器天然是“面向交易日”的调度，不是通用 cron 调度器

---

## 9. 聊天业务

系统里其实有两种聊天：

- 无状态聊天
- 持久会话聊天

### 9.1 无状态聊天

接口：/chat 和 /chat-stream

业务特点：

- 只使用请求里传入的 messages
- 不持久化到 chat_sessions
- 适合轻量问答或兼容旧接口

### 9.2 持久会话聊天

接口：

- /chat/sessions
- /chat/sessions/{id}/messages
- /chat/stream
- /chat/uploads

业务特点：

- 每条用户消息都会落库
- 会话标题可自动从首条内容中推导
- assistant 返回也会落库
- 支持附件参与上下文

### 9.3 持久聊天的完整流程

1. 前端选择一个 user 类型会话
2. 用户输入文本或上传附件
3. 后端先把 user message 落库
4. 读取该会话全部历史消息
5. 构造 history_messages
6. 调用 llm_service.chat 开始流式输出
7. 前端按 SSE 更新正在生成的 assistant 内容
8. 结束后把 assistant message 落库

业务含义：

- 会话消息持久化是在模型执行前就开始的，不是等全成功后再写
- 所以即使中途中断，历史也会保留部分业务痕迹

### 9.4 聊天失败时的业务处理

如果聊天执行失败：

- assistant 会写入一条失败内容
- 内容格式为 执行失败：错误原因

如果客户端断开：

- 会标记为执行中断

业务含义：

- 用户聊天历史不追求“只有成功才留痕”
- 失败也是上下文的一部分

### 9.5 附件业务规则

附件上传遵循这些规则：

- 单文件最大 10MB
- 单条消息最多 12 个附件
- 仅允许图片、文本类文件及部分现代办公文档
- 对文本类附件会尝试抽取正文
- 对图片类附件会通过 URL 或 data reference 参与上下文

业务含义：

- 附件并不是简单保存下载，而是被设计成模型上下文的一部分
- 如果要支持新的文件类型，核心不只是白名单，还要补正文提取逻辑

---

## 10. 技能系统业务

### 10.1 技能系统的角色

技能系统是大模型可以调用的工具能力集合。

它分两层：

- 原生技能，直接提供 Python tool handler
- 文档驱动技能，只提供说明，由运行时工具配合执行

### 10.2 技能来源

当前技能来源有两类：

- builtin: 系统内置技能
- workspace: 用户导入到工作区的技能

业务含义：

- 内置技能是系统能力基座
- workspace 技能是扩展能力

### 10.3 系统运行时技能

存在一类特殊技能：system runtime skill。

它们：

- 始终视为启用
- 不能被禁用
- 为其他技能提供底层通用能力

业务含义：

- 这类技能不是普通插件，而是平台运行时的一部分

### 10.4 技能启停规则

用户禁用的技能不会从磁盘删除，而是：

- 写入 app_settings.disabled_skill_ids_json
- skill_registry 重新加载后按该集合过滤可用技能

业务含义：

- 禁用是运行时状态，不是卸载
- 技能状态恢复依赖 settings 持久化

### 10.5 技能如何参与模型决策

模型侧的工具定义来自 skill_registry.build_tools。

业务流程：

1. 根据 run_type 收集可用技能
2. 构造 tools 定义给大模型
3. 大模型返回 tool_call
4. skill_registry.execute_tool 执行对应 handler
5. 结果写回 tool_calls 与 runtime_trace

业务含义：

- 技能启停会直接改变模型可见工具集合
- run_type 也会影响模型看到哪些工具

如果你发现某个工具“存在但模型没调用”，要检查三件事：

1. 技能是否启用
2. 技能是否支持当前 run_type
3. 提示词是否让模型有足够动机使用它

### 10.6 技能导入业务

支持三种导入方式：

- SkillHub
- ClawHub
- zip 包

业务规则：

- 导入包有大小上限
- 解压内容有总大小和文件数安全限制
- 工作区技能 id 不能与内置技能冲突
- 默认不支持覆盖导入

业务含义：

- 导入系统首先考虑的是运行安全和目录边界安全

---

## 11. LLM 执行业务

### 11.1 LLM 服务在系统中的职责

llm_service 不是简单调用模型接口，它承担了：

- 构建系统提示词
- 拼接技能补充说明
- 提供工具定义
- 驱动模型和工具之间的循环
- 处理流式输出
- 将上游异常转成更可读的业务错误

### 11.2 系统提示词的组成

实际送给模型的系统信息不只有 settings.system_prompt，还会叠加：

- 技能运行时说明
- 已启用技能摘要
- 常驻技能的 SOP 文本
- 聊天模式下的安全确认规则

业务含义：

- 想改模型行为，不一定非得改 settings.system_prompt
- 很多行为来自技能运行时补充说明

### 11.3 任务运行与聊天运行的差异

聊天模式和任务模式虽然都走 LLM，但业务目标不同：

- 聊天模式强调安全确认与会话交互
- 任务模式强调自动执行与工具调用链

这意味着：

- 某些提示词修改可能只应作用于 chat
- 某些工具应该只对 trade 或 analysis 开放

---

## 12. 事件流与前端实时展示业务

### 12.1 run-stream 的业务逻辑

手动触发流式运行时：

1. 后端立即返回 run_id
2. 实际任务在线程中后台执行
3. 执行过程中的事件通过 EventBus 发布
4. 前端通过 /runs/{run_id}/events 订阅 SSE

EventBus 的业务意义：

- 支持运行中实时展示阶段变化、工具调用、交易动作和完成状态
- 即使订阅稍晚，也可以通过短期 replay history 补上已发出的事件

### 12.2 为什么前端能看到实时调用细节

因为运行过程中会持续 emit 事件，例如：

- stage
- tool_call
- trade_order
- completed
- failed

业务含义：

- 任务页显示的“实时接口调用”和“实时交易执行”不是事后推导，而是运行中直接广播

---

## 13. 页面业务语义

### 13.1 总览页

总览页关注三类信息：

- 账户状态
- 当前持仓和委托
- 最近运行效果

业务目的：

- 给操作者一个“当前账户与策略状态”的综合面板

### 13.2 AI 分析页

分析页不只是展示文本结论，它真正展示的是：

- 运行历史
- 当前运行状态
- 工具调用轨迹
- 交易动作
- 分析输出正文

业务目的：

- 让一次运行可审计、可回放、可删除

### 13.3 功能设置页

设置页实际上承载了两块业务：

- 模型和妙想配置管理
- 技能生命周期管理

业务含义：

- 它不是单纯的偏好设置页，而是系统运行控制台的一部分

---

## 14. 业务失败路径与兜底逻辑

### 14.1 任务失败

任务失败时会发生：

1. StrategyRun.status 置为 failed
2. error_message 被保存
3. 自动化会话中写入失败 assistant 消息
4. 如果是定时任务，进入 retry 逻辑

业务含义：

- 失败不是静默丢失，而是业务状态的一部分

### 14.2 账户接口失败

账户接口失败时：

- 优先回退到最近任务的快照
- 同时把错误加入 errors 字段

业务含义：

- 页面仍能工作，但会明确告诉用户当前是缓存数据

### 14.3 聊天失败

聊天失败时：

- 流结束
- assistant 留下失败说明
- 历史消息保留

### 14.4 技能执行失败

技能执行失败时：

- execute_tool 返回 ok = false
- 错误进入工具结果
- 最终是否导致整轮失败，取决于上层模型与执行流如何消费这个结果

业务含义：

- 工具失败不总是等价于任务失败
- 它可能只是让模型改用其他工具或给出保守结论

---

## 15. 最适合修改业务逻辑的切入点

### 15.1 想改“任务怎么做”

优先看：

- aniu_service._prepare_run
- aniu_service._run_body
- llm_service
- skill_registry

### 15.2 想改“下一轮如何记住上一轮”

优先看：

- _get_or_create_persistent_session
- _build_persistent_session_user_content
- _build_persistent_session_assistant_content
- _maybe_compact_persistent_session

### 15.3 想改“账户页展示的口径”

优先看：

- get_account_overview
- _build_account_response
- _build_account_overview
- _build_orders_overview
- _build_trade_summaries

### 15.4 想改“调度和失败重试”

优先看：

- process_due_schedule
- _compute_next_run_at
- _run_body 的失败分支

### 15.5 想改“聊天的行为和附件规则”

优先看：

- chat_session_service.stream_chat
- save_attachment
- _extract_attachment_text
- llm_service.chat

### 15.6 想改“技能如何被模型看到和调用”

优先看：

- skill_registry.build_tools
- skill_registry.execute_tool
- skill_registry.build_prompt_supplement
- skill_admin_service

---

## 16. 一句话总结每条主链

认证链：

- 环境变量密码登录，签发 JWT，保护整个单用户实例。

设置链：

- 数据库中的唯一设置对象控制模型、妙想和自动化行为。

账户链：

- 实时接口、运行快照和进程缓存三层融合，优先保证页面可用性。

运行链：

- 一次任务从 StrategyRun 开始，经过模型和技能调用，最后沉淀为结论、交易动作和上下文记忆。

调度链：

- 面向交易日的单任务轮询调度，失败时做有限次延迟重试。

聊天链：

- 用户会话是持久化的流式交互，附件和失败信息都是上下文的一部分。

技能链：

- 技能系统决定模型能用什么工具，也决定业务扩展能力的边界。

自动化记忆链：

- 所有任务共享一个自动化会话，通过摘要压缩维持长期连续决策能力。

---

## 17. 二次开发建议顺序

如果你的目标是“修改业务逻辑”，建议优先按下面顺序动手：

1. 先确认你改的是哪条业务链
2. 找出该链路的状态对象和落库对象
3. 明确这条链的成功路径和失败路径
4. 再决定修改 service、schema、前端展示还是提示词
5. 最后补对应测试，避免把已有业务规则悄悄改坏

最关键的一条原则：

先改业务规则，再改展示；先改状态流转，再改页面文案。

否则很容易做成“界面变了，但核心行为没变”或者“行为变了，但审计和展示没跟上”。