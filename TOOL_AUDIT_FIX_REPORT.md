# 工具调用审计修复报告（2026-10-30，外部审计六补丁实施）

## 背景

用户提供了一份针对工具调用模块的外部审计（基于公开 main 分支源码的静态审计），
指出"模型表达意图、工具计划、实际外部操作和结果确认"之间缺少统一、不可绕过的
状态约束。本次先逐条对照代码验证审计判断（全部属实），然后按其优先级实施修复。
提示词部分（审计第九节）由用户自行处理，本次不改动。

## 审计判断验证结果（全部属实）

| 审计判断 | 验证 |
|---|---|
| BUY 预检不查同标的未完成买单 | 属实：`preflight_trade` BUY 分支只查资金 |
| 交易超时后状态不明，下一轮可重复提交 | 属实：`client.trade` 无幂等键，超时按失败处理 |
| 一致性 autofill 可由自然语言自动补出真实委托/自选股变更 | 属实（含一个审计未发现的更隐蔽问题，见补丁 A） |
| 自选股操作不回查远端列表，接口成功≠变更完成 | 属实：`_handle_manage_self_select` 直接 `ok=True` |
| 撤单/自选股写操作允许 2 次自动尝试 | 属实：只有 BUY/SELL 限 1 次 |
| `_mark_action_executing` 非原子（读-判-写） | 属实 |
| `replace_plan` 无条件删除已有动作 | 属实 |
| `execute_tool` 执行时不复检 run_type 权限 | 属实：分析任务幻觉调用交易工具会被真实执行 |
| HTTP 200 + 业务失败被记成 success 日志 | 属实：`_raise_on_business_error` 在日志上下文外调用 |
| 聊天入口绕过计划执行层 | 属实但按补丁 F 第三项方式修复（见下） |

## 已实施修复

### 补丁 A：禁用自然语言自动补写（P0，审计第一优先级）

`aniu_service.py`：
- `_enforce_trade_consistency` / `_enforce_self_select_consistency` 的 autofill
  分支整体移除：检测到声明与计划不一致时只标记 `consistency_gap`、保留
  `original_final_answer` 审计、请求模型 followup 纠正（模型可自己补调工具，
  走全部挡板）——**后端不再由文字生成任何写操作**。
- **额外发现并修复**：自选股一致性分支开头"只要分析"的调用实际通过
  `_build_self_select_consistency_autofill_tool_calls` 内部的
  `_materialize_consistency_operations` **执行真实操作**——比 autofill 分支
  更隐蔽的写路径。已替换为纯分析 dict。
- 删除三个死函数：`_materialize_consistency_operations`、两个 autofill builder。
- `_extract_claimed_self_select_changes` 不再把"新增候选/新增关注/新增重点跟踪"
  等研究性表述解释为自选股新增声明（审计第四节要求）。
- followup 合并处补 `consistency_gap` 标记（原先丢失）。

### 补丁 B：自选股操作意图解析 + 前后状态核验（P0）

`skills/mx_core/execution.py`：
- 新增 `_parse_self_select_intent`：query 必须唯一解析出
  ADD/REMOVE + 六位代码或名称，否则**拒绝执行且不发送任何写请求**
  （审计："无法唯一确定就拒绝，不能回退使用整段 task_prompt"）。
  目标片段提取与单目标校验共用 `_SELF_SELECT_TARGET_PATTERNS`。
- 操作前读取 `mx_get_self_selects`：目标已处于期望状态 → NOOP_VERIFIED，
  不发送写请求（幂等，防重复 ADD/REMOVE）。
- 操作后回查：目标状态确认 → `verified=True`；回查失败或未确认 →
  UNKNOWN 错误（指引模型调用 mx_get_self_selects 核对，禁止自动重发）。
- 名称与代码两种写法解析为同一意图（审计清单 #8）。

### 补丁 C（预检层）：BUY 未完成买单拦截 + 超时状态未知

`skills/mx_core/execution.py`：
- `preflight_trade` BUY 分���新增：同标的存在未完成买单（pending 状态且
  remaining>0）时拒绝再次买入——跨轮重复买入的持久化挡板（审计第三节 1）。
  卖单/已成交买单不影响。
- `_handle_moni_trade` 捕获 `httpx.TimeoutException/TransportError`：
  报"委托状态未知（UNKNOWN）"并指引先查 `mx_get_orders` 核对，**绝不自动
  重发**（审计第三节 2）。下一轮同标的同方向买入由未完成买单预检拦截。
- 注：审计建议的独立交易意图表（intent_key 唯一约束）未实施——预检层
  挡板已覆盖其主要目标（同标的同方向重复委托），意图表需要稳定的
  signal_id/tranche_id 语义设计，建议后续单独评审。

### 补丁 D：replace_plan 保护已发送动作

`execution_runner_service.py`：`replace_plan` 只删除仍处于 `planned` 的
动作；executing/completed/failed 动作是不可变的审计记录，跳过删除并告警。

### 补丁 F：写操作单次尝试 + 原子抢占 + 执行时权限复检 + 日志修复

- `_resolve_action_max_attempts`：CANCEL/MANAGE_SELF_SELECT 与 BUY/SELL
  一样只允许 **1 次**自动尝试（原 2 次）；读取类动作仍可重试。
- `_mark_action_executing` 改为**原子条件更新**（`UPDATE ... WHERE status IN
  (planned, failed_retryable)` + rowcount 校验），消除并发抢占窗口。
- `skill_stack_service.execute_tool` 新增**执行时权限复检**：按 context 的
  run_type 重新过滤工具，分析任务中幻觉调用 `mx_moni_trade` 等写工具直接
  拒绝（此前只过滤工具列表、不过滤执行）。
- 聊天入口统一：审计建议路由到计划层，但那会让聊天写工具只生成计划不
  执行、破坏聊天语义；实际采用"执行时权限复检"（补丁 F 第三项原文），
  聊天（chat profile 含交易工具）保持即时执行但受全部执行层挡板约束。
- `MXClient._post`：业务错误检查移入日志上下文内，HTTP 200 + 业务失败
  （status!=0）现在落成 **failed** 日志（`instrument_invocation.observe`
  新增 error_summary 参数）。

## 测试

新增 `tests/test_tool_audit_fixes.py`（20 个）：
- BUY 预检：同标的未完成买单拦截 / 卖单不拦 / 已成交不拦 / 他标的不拦
- 交易超时：状态未知提示 + 只发送一次不重发（审计清单 #4）
- 原子抢占：planned 可抢占、completed 不可、executing 不可重入（#11 单进程）
- replace_plan：planned 可替换、executing/completed 保留（#10 部分）
- 写操作单次尝试：CANCEL/MANAGE_SELF_SELECT/BUY/SELL 均 1 次（#9 部分）
- 执行时权限：分析任务调用交易工具被拒、交易任务通过权限层（#14 部分）
- 日志：HTTP 200 + status=100 落成 failed 日志
- 自选股回查：ADD 回查确认 / 已在列表 NOOP / 不在列表 REMOVE NOOP /
  接口成功但未生效 → UNKNOWN / 回查失败 → UNKNOWN / 模糊意图拒绝且零写请求 /
  名称与代码同意图（#8）/ ADD-REMOVE-ADD 合法序列（#7）

重写旧行为测试 7 个（autofill 单元 4 + E2E 2 + 重试次数 1）为断言新行为。

**全量回归：后端 371 passed**（351 + 20 新增）。前端无改动。

## 第二轮：补丁 C / D / E 全部实施（2026-10-30 复审后）

用户复审认为 C/E/D 三项分别处理"操作重复 / 工具完整性 / 结果真实性"三个
层面、不可互相替代且均为必须实施。本轮全部落地：

### 补丁 C：独立交易意图表（trade_intents）

- **模型**：`TradeIntent`（signal_date/symbol/side/tranche_id 唯一约束 +
  intent_key 唯一索引 + provider_order_id + 状态机）。
- **signal_id 语义**：后端按（上海交易日, 标的, 方向）派生——同一天同一
  标的同一方向视为同一信号，15 分钟任务反复触发不产生新键；**不由模型生成**。
- **tranche_id**：前一档 FILLED 且 45 分钟冷却期已过才允许新档；每日每标的
  每方向上限 3 档（含失败重试档）——防重复挡板不会变成禁止合理加仓的死规则。
- **覆盖 BUY/SELL/CANCEL**：撤单意图按委托单号全局唯一（一单一个撤单意图，
  失败意图复用原行重试）。
- **UNKNOWN 处理**：超时/传输中断 → 意图置 UNKNOWN 阻塞同标的同方向；下次
  注册前自动 `reconcile_unknown_intents`——委托列表有痕迹（按单号/方向唯一
  匹配）→ 收敛 submitted/filled/partially_filled；无痕迹 → failed 解除阻塞；
  无法判断 → 保持阻塞。at-most-once 发送 + 状态核验，绝不盲目重发。
- **启动恢复**：`app.main` 启动时把遗留 EXECUTING 意图收敛为 UNKNOWN
  （阻塞直到核对），不会因重启把未完成操作当作新操作重新执行。
- **接线**：`_handle_moni_trade` 在 preflight 后注册意图（成功→submitted、
  超时→UNKNOWN、业务拒绝→failed）；`_handle_moni_cancel` 注册撤单意图并
  在撤单后回查委托状态（撤单请求 ≠ 撤单完成）。

### 补丁 D：后端权威执行结果

`_build_backend_execution_report`：后端根据工具计划、执行记录、回查状态
生成权威报告，追加到最终结论之后并落库（decision.backend_execution_report）。
状态口径：SUBMITTED（交易/撤单）、VERIFIED / NOOP / UNKNOWN（自选股回查）、
NOT_CALLED（声明未获执行确认）、BLOCKED_MISSING_DATA（数据门禁拦截）、
FAILED/UNKNOWN（未落地动作）。模型负责解释为什么操作，后端负责证明操作
究竟有没有发生。前端结构化展示（执行卡片）按用户意见后置。

### 补丁 E：必需工具调用检查

- 代码级清单（不依赖提示词）：analysis 必需 mx_get_self_selects /
  mx_search_news / mx_screen_stocks；trade 必需 mx_get_balance /
  mx_get_positions / mx_get_orders / mx_get_self_selects。写工具是条件性的，
  不强制。
- **覆盖全部轮次**：检查基于合并后的 tool_calls（含一致性 followup 与
  补充调用的结果），不只看最后一次响应。
- 缺失时给模型**一次补充调用机会**（补充调用的工具结果真正进入后续
  计划执行流程）；仍缺失 → `data_validation_passed=False`，本轮**全部
  写操作计划被拦截**（不进入执行器），拦截原因记录在
  decision.blocked_actions_missing_required_tools。
- 动态技能（tdx-stock-api 等）工具名由部署环境决定，暂未纳入强制清单。

### 补充防护（用户复审指出）

- **自选股并发锁**：读取→判断→写入→回查全程模块级互斥，防止两个并发
  任务同时读到"不存在"然后同时发送 ADD。
- **SELL 信号重复**：意图表对 SELL 与 BUY 对称生效（同一退出信号反复
  出现只允许一笔在途）。
- **run_type 缺失**：执行时权限复检对缺失 run_type 默认按 analysis 处理
  （交易工具被拒），不会因缺失而放行写操作。

### 第二轮测试

- `tests/test_write_operation_safety.py`（15 个）：意图去重/冷却/分批上限/
  SELL 对称/次日新信号/撤单唯一/崩溃恢复/并发唯一约束/UNKNOWN 两种收敛/
  失败重试 + 补丁 D 报告全状态与空运行。
- E2E：必需工具缺失时交易计划被拦截、零动作执行、最终结论附带
  BLOCKED_MISSING_DATA 报告（`test_automation_session.py`）。
- 既有 E2E 的假 agent 全部补上必需读取工具调用（补丁 E 上线后的正确
  测试形态——不调用必需工具的运行本来就该被拦截）。

**全量回归：后端 387 passed**（371 + 16 新增）。

### 第三轮：补丁 D 前端执行卡片（2026-10-30）

- **后端补充**：`_build_execution_report_payload` 产出机器可读的结构化
  报告（schema=backend_execution_report_v1），与文本报告同源同口径，存入
  decision_payload.execution_report——前端卡片不再解析自然语言。
  字段：overall_status（NO_ACTION/SUBMITTED/VERIFIED/UNKNOWN/NOT_CALLED）、
  trades/cancels/self_selects（含逐条状态）、not_called_claims、
  blocked_actions、unresolved_count、missing_required_tools、
  data_validation_passed。
- **前端 composable**（useAnalysisRuns.ts）：`BackendExecutionReportViewModel`
  解析 + 状态枚举中文映射（已提交/已回查确认/已满足（未发送）/状态未确认/
  声明未执行/已拦截/本轮无写操作），空值与畸形字段全部防御。
- **TasksView**：新增"执行结果（后端权威记录）"卡片，与执行概览/动作
  时间线并列——顶部总体状态徽章 + 权威口径提示，分组渲染交易执行、撤单
  请求、自选股变更（逐条状态）、声明未获执行确认（NOT_CALLED）、数据门禁
  拦截（BLOCKED_MISSING_DATA），尾部提示未落地动作数与必需工具缺失。
  显示条件：detail 加载且存在 execution_report（不依赖动作记录）。
- **验收**：浏览器 E2E——种子运行（含全部状态类型）渲染出完整卡片，
  各状态徽章/分组/提示与后端 payload 一一对应；截图留档。
- 测试：后端 payload 结构 2 个（全状态/空运行）；前端 2 个
  （卡片渲染断言 + composable 解析断言）。
  **全量回归：后端 389 passed，前端 86 passed，vue-tsc/build 通过。**

### 第四轮：复审 P0/P1/P2 修复（2026-10-30 第二次外部复审）

**P0-2 异常分类**（`skills/mx_core/client.py` + `execution.py`）：
新增 `MXBusinessRejected`（远端明确拒绝：业务错误 status/code/错误体）
与 `MXResponseUnknown`（HTTP 200 但响应体无法解析）。交易/撤单 handler
按类型分流：业务拒绝 → 意图 failed；响应不确定/超时/传输中断 → 意图
UNKNOWN（阻塞）。**所有 RuntimeError 一律标 FAILED 的错误路径已消除。**

**P0-1 UNKNOWN 恢复**（`trade_intent_service.py`）：
- 委托列表解析与 `execution._payload_rows` 同源（含 **orderList**——
  复审发现的解析器不一致会让 orderList 响应被误判"无痕迹"从而错误
  解除阻塞）。
- 载荷结构无法识别 → 不可判定，保持阻塞且不消耗核验次数。
- **可见性等待**：查不到痕迹 ≠ 未提交——需连续 ≥2 次独立核验且超过
  90 秒等待期才收敛 failed；此前保持 UNKNOWN 阻塞（新增
  unknown_checks / first_unknown_at 字段）。
- 无单号时按 标的+方向 唯一匹配（多候选不可判定）。

**P0-3 状态闭环**：
- `reconcile_open_trade_intents`：SUBMITTED/PARTIALLY_FILLED/UNKNOWN
  按远端委托列表同步为 filled / partially_filled / canceled / submitted
  （全部成交 → filled 进入冷却；部分成交 → 继续阻塞；已撤/废单 →
  终态放行）。
- `reconcile_all_open_intents`：**每轮 Trade 任务开始前**（client 创建
  后、LLM 前）同步全部在途意图——成交冷却与分批规则基于真实订单状态。
- 技术性重试（failed 档）单独上限 3 次，**不消耗真实分批档位**。
- 启动恢复加 **run 状态守卫**：只收敛所属 run 已非 running 的意图，
  多 worker 下不误判另一 worker 正在执行的订单。

**P1 追溯与数据完整性**：
- run_id 贯通：execution_runner → mx execute_tool → handler → 意图注册；
  成功执行后回填 action_id（交易 → 模型决策 → 计划动作追溯链）。
- **memory_read 逐标的检查**：trade 运行中每个拟 BUY/SELL 标的必须
  在成功的 memory_read 关键词中出现过（检索结果为空是有效结果，
  完全没检索则数据验证不通过）。
- **载荷完整性校验**：必需读取工具 ok=True 但 result 载荷不可识别
  （空 dict/无数据键）视为未拿到有效数据，等同缺失。

**P2 报告增强**：
- 总体状态新增 **INCOMPLETE**（无写操作但必需数据缺失，不再显示成
  正常完成的无操作分析）。
- `unresolved_actions` 逐笔详情（标的/动作/数量/原因），前端卡片
  从"未落地 N 条"升级为逐笔列表。

**复审专项测试**（清单 #1-#5/#7/#9，新增 7 个）：UNKNOWN 先不可见后
出现（#1）、orderList 解析（#2）、响应无法解析 → UNKNOWN（#3）、
SUBMITTED→FILLED 同步（#4）、部分成交后撤销终态（#5）、启动恢复
跳过 running run（#7）、缺 memory_read 拦截拟交易（#9）+ 覆盖通过 +
空载荷视为缺失。

**全量回归：后端 399 passed，前端 86 passed，vue-tsc/build 通过。**

### 第五轮：第三轮复审 P0×5 + P1×3 修复（2026-10-30 第三次外部复审）

**P0-1 分状态核验 + 终态白名单**：
- SUBMITTED/PARTIALLY_FILLED 意图在委托列表查不到时**保持原状态阻塞**
  （委托消失 ≠ 未提交——可能已成交/撤销/转历史），绝不进入"无记录
  等待→failed"路径；只有 UNKNOWN（发送状态不明、无单号）才允许经
  可见性等待判定未提交。
- `_order_terminal_status` 改为**终态白名单**：未知状态码返回
  unknown（保持阻塞），不默认 canceled。

**P0-2 单号精确匹配**：有 provider_order_id 时只做单号精确匹配，
找不到**绝不退化为标的+方向匹配**——防止把同标的的其他委托
（ORD-099）认作本意图（ORD-100）而错误收敛。无单号时才允许
标的+方向联合匹配，多候选不自动选择。

**P0-3 异常分类补全**：
- 字符串业务状态（status="100" / code="100"）→ MXBusinessRejected
  （原为普通 RuntimeError，意图会卡在 executing）。
- HTTP 4xx → MXBusinessRejected（远端明确拒绝）；HTTP 5xx →
  MXResponseUnknown（请求已送达但处理结果未知，不放行重试）。
- 交易/撤单 handler 增加**未分类异常兜底**：请求发出后的一切未分类
  异常一律按状态未知处理（意图 UNKNOWN），绝不放行。

**P0-4 followup 后完整重验**：提取 `_validate_required_tool_coverage`
（静态工具 + 逐标的 memory_read 完整校验），followup 合并后**重新执行
全部检查**——修复"模型补充静态工具却仍跳过 memory_read 时门禁被绕过"
的漏洞。

**P1 三项**：
- **撤单意图状态闭环**：`reconcile_cancel_intents` 按原委托单号同步
  （已撤销→verified、已成交→filled、仍待撤→submitted、消失/不可判定
  →unknown 阻塞重复撤单），并入 `reconcile_all_open_intents` 每轮同步。
- **action_id 精确关联**：回填按 run_id + 标的 + 方向联合匹配——
  同一轮多笔交易分别关联各自动作，不再串档。
- **data-list 兼容**：`_extract_order_rows` 支持 data 为列表
  （与 execution._payload_rows 完全同源）。

**第三轮专项测试**（清单 12 项中可自动化的 11 项，新增 11 个）：
字符串 status/code 拒绝（#1/#2）、HTTP 500 → UNKNOWN（#3）、
已成交委托消失不判 failed（#4）、单号不匹配保持 UNKNOWN（#5）、
未知状态码保持 UNKNOWN（#6）、followup 缺 memory_read 仍拦截（#7）、
同 run 两笔交易 action_id 分别关联（#8）、撤单状态闭环（#9）、
data 列表解析（#11）、HTTP 4xx 拒绝。清单 #10（进程崩溃租约）与
#12（45 分钟新信号条件）属租约架构与策略语义设计，见已知边界。

**全量回归：后端 410 passed，前端 86 passed，vue-tsc/build 通过。**

### 第六轮：第四轮复审 P0×4 + P1×2 修复（2026-10-30 第四次外部复审）

**P0-1 预检 Fail-Closed**：`_payload_rows_with_state` 四态解析
（VALID_DATA / VALID_EMPTY / INVALID / ERROR）。BUY/SELL 预检经
`_require_parseable_rows`：结构无法识别一律拒绝交易——原实现静默返回
空列表 = Fail-Open，异常响应会被当作"无在途委托"放行。

**P0-2 未知状态保守阻塞**：BUY/SELL 预检遇到未知状态码且剩余数量
大于零或不明（ordered<=0）时阻断新委托——不能因状态不认识就当作
非在途订单放行。

**P0-3 UNKNOWN 解除需完整订单视图证据**：活动委托列表查不到不能
证明未提交（订单可能已成交离开列表）。可见性等待（≥2 次 + 90 秒）
之外新增证据要求：**委托列表必须包含历史委托（canceled/filled 痕迹）**
才构成"当日全量视图"，此时"无痕迹"才可判定未提交；否则 UNKNOWN
保持阻塞等待人工核验。

**P0-4 数量缺失不判成交**：`_order_terminal_status` 增加
ordered<=0 → unknown、dealt>ordered → unknown——修复"委托数量缺失时
dealt>=ordered 恒真导致误判 filled"的边界错误。

**P1-1 撤单部分成交**：原委托部分成交、剩余在途 → 撤单意图
CANCEL_PENDING（保持 submitted 阻塞重复撤单但**不锁死**剩余部分的
后续处理），不再误归 filled。

**P1-2 intent_id 精确关联**：交易/撤单 handler 把 intent.id 放入
executed_action，回填优先按 intent_id 一对一更新——同标的两笔同方向
不同数量的交易分别关联各自动作，不再按标的+方向批量 UPDATE 串档。

**HTTP 408/425/429**：超时/限流类 4xx 归 MXResponseUnknown（请求
可能已送达），不按业务拒绝放行重试。

**第四轮专项测试**（新增 9 个）：无法识别结构阻断 BUY/SELL 预检（#1）、
未知状态在途单阻断（#2）、数量缺失→unknown（#3）、成交超量→unknown、
intent_id 一对一关联（#6/#8）、部分成交撤单 CANCEL_PENDING（#7）、
HTTP 408→unknown（#8）、无历史视图 UNKNOWN 保持阻塞（#10）。
清单 #4/#5 已有测试覆盖；#9（第三档新行情信号）属策略语义，见边界。

**全量回归：后端 419 passed，前端 86 passed。**

### 第七轮：第五轮复审 P0×4 + P1×3 修复（2026-10-30 第五次外部复审）

**核实项：intent_id 执行器链路**——回填代码实际存在（复审读到旧快照），
本轮补齐 `_normalize_executed_action` 保留 intent_id + UPDATE rowcount
校验（≠1 记录告警）。

**P0-1 订单行级 Schema 校验**（结构校验 → 字段级校验）：
- `_row_int_optional`：数量字段缺失/格式异常返回 None——缺失值不再与
  真实 0 混淆（待处理委托数量缺失 = 剩余不可判定）。
- `_validate_order_row`：缺股票代码 / 缺状态码（显式原始字段检查，
  `_order_status` 缺省 "0" 不算有效值）/ 待处理但缺 orderCount 或
  dealCount → 该行无效。
- `_require_parseable_order_rows`（委托列表专用）：结构级 + 行级校验，
  任一行无效 → 整个列表按 INVALID 拒绝。持仓/自选列表用通用
  `_require_parseable_rows`（仅结构级）——订单字段语义不适用于持仓行。
- `data: [null, "invalid", 123]` → INVALID（列表含非 dict 元素）。

**P0-2 待处理但数量缺失 → 阻断**：BUY/SELL 预检中待处理状态 +
orderCount/dealCount 缺失 → 直接阻断（原实现 remaining=0 被放行）；
SELL 在途占用推算路径同样处理。

**P0-3 UNKNOWN 一律不自动解除**（证据标准重设计）：采纳复审"一条
历史订单 ≠ 完整历史视图"的判断——活动委托列表查不到不能证明未提交，
partially_filled 可能仍在活动列表，分页/过滤/延迟均无法排除。在妙想
提供可验证的完整历史委托视图（明确交易日期、分页总数、has_more、
查询成功状态）之前，UNKNOWN 保持阻塞等待人工核验或订单重新可见。
原"历史视图可解除"逻辑整体移除。

**P1 三项**：
- **撤单回查四态**：`_verify_cancel_result` 改用 Fail-Closed 解析，
  查询失败/结构异常/找不到订单一律 UNKNOWN——"找不到"不再描述为
  "通常表示撤单已生效"。
- **preflight_cancel 统一 Fail-Closed**：委托列表解析与 BUY/SELL 同源。
- **cancel_pending 独立状态**：原委托部分成交、剩余在途 → 撤单意图
  进入独立 `cancel_pending` 状态（区别于 submitted），不锁死剩余部分
  的后续处理。

**第五轮专项测试**（新增 11 个）：data 含非法元素 → INVALID（#1）、
待处理缺 orderCount/dealCount → 行级 INVALID（#2/#3）、缺状态码 →
INVALID（#4）、缺代码 → INVALID、预检层待处理缺数量阻断 BUY/SELL（#2/#9）、
仅 partially_filled 不证明历史视图（#5）、撤单回查结构异常/找不到 →
UNKNOWN（#7）、`_row_int_optional` 区分缺失与 0。

**全量回归：后端 429 passed，前端 86 passed，vue-tsc/build 通过。**

### 第八轮：第六轮复审 P0×5 + P1×4 修复（2026-10-30 第六次外部复审）

**公共订单 Schema 模块**（`skills/mx_core/order_schema.py`，P0-4 核心）：
execution.py 与 trade_intent_service.py 此前各持一套解析器（宽松过滤
非法行、缺失数量默认 0）——同一份订单数据两处得出不同结论。现在统一
提供 parse_order_snapshot / validate_order_row / get_order_quantities /
get_order_status / get_order_side / order_terminal_status，两处同源。

**P0-1 方向字段校验**：订单行必须含方向字段且在白名单
（1/2/BUY/SELL/B/S）——缺 orderDrt 的在途委托此前会被 BUY 当非卖单、
被 SELL 跳过，可能低估可卖占用。方向缺失 → 整份列表 INVALID。

**P0-2 数量合法性**：待处理订单 ordered>0、0≤dealt≤ordered；
row_int_optional 拒绝非整数（100.9 股不再静默截断为 100）。
orderCount=0/-100、dealCount=-1、dealt>ordered → INVALID。

**P0-3 BUY 对 status=0 统一未知处理**：BUY 此前对 "0" 直接放行
（与 SELL 不一致）——现在与 SELL 一致：剩余不明或大于零即阻断。

**P0-5 无单号 UNKNOWN 禁止模糊匹配**（本轮最重要的错误关联修复）：
超时订单没有 provider_order_id，唯一同标的同方向候选也可能是**历史
订单**（09:30 已成交的 ORD-001 会被认作 10:00 超时的新订单，错误收敛
FILLED/CANCELED 解除阻塞）。现在无单号一律不匹配，保持 UNKNOWN 等
待人工核验；只有单号精确匹配才同步状态。

**P1 四项**：
- intent_id 回填加 **run_id 校验**（防跨 run 错误关联）+ **移除批量
  fallback**（缺 intent_id 不做标的+方向模糊回填，记录告警）。
- 撤单回查终态化：用 order_terminal_status 区分 VERIFIED（已撤）/
  FILLED（全部成交）/ CANCEL_PENDING（仍待撤/部分成交）/ UNKNOWN
  （不可判定）——未知状态不再描述为"已撤单或已成交"。
- **MAX_TRANCHES_PER_SIGNAL = 1**（复审特别建议）：45 分钟冷却无独立
  新信号验证能力，保守模式防止"时间到了就允许二次进攻"；恢复分批
  需后端可验证的新行情确认条件。
- intent_id 精确关联的 E2E 语义测试（跨 run 拒绝、缺 intent_id 不回填）。

**第六轮专项测试**（新增 11 个）：缺方向 → INVALID（#1）、
orderCount=0/-100、dealCount=-1、dealt>ordered → INVALID（#4-#7）、
非整数数量拒绝、BUY status=0 阻断（#8）、SELL 方向缺失阻断（#9）、
无单号 UNKNOWN 不认领历史订单（一笔/两笔，#10/#11）、有单号但行
INVALID → 保持 UNKNOWN（#12）、跨 run intent_id 拒绝关联（#13）。

**全量回归：后端 440 passed，前端 86 passed，vue-tsc/build 通过。**

### 已知边界（如实记录）

- signal_id 是日级标识（交易日+标的+方向），45 分钟冷却放行第二档
  是**基于时间的规则**而非验证出独立新信号——严格防重复可改为
  "每日同标的同方向一笔"保守模式（改一个常量即可），保留当前模式
  是为不扼杀短线二次进攻；后续可引入策略信号生命周期。
- 自选股互斥锁是进程内线程锁：单 worker 部署安全；多 worker 需
  数据库级互斥（当前以单 worker 部署为约束）。
- tdx-stock-api 等动态技能的必需工具映射待部署环境确认工具名后
  纳入（本地未部署该技能，无法验证）。
- 委托状态码语义：终态白名单（撤单/废单编码）基于常见约定推断，
  未知码一律 UNKNOWN 保持阻塞（安全默认），需真实接口验证后修正。
- 进程租约（worker_id/heartbeat/lease_expires）未实现：当前以
  单 worker 部署为约束；run 状态守卫覆盖正常启动竞争，强杀进程
  （run 停留 running）的意图需等 run 状态收敛后才会恢复。
- 45 分钟冷却无独立新信号验证：**已按第六轮复审建议将
  MAX_TRANCHES_PER_SIGNAL 降为 1（保守模式）**。恢复分批（改回常量）
  需后端可验证的新行情确认条件（策略语义设计）或你的明确批准。
- 委托列表已实现行级 Schema 校验（第五轮）；持仓/自选/余额的字段级
  Schema 校验未实施——需真实接口字段语义确认后建立。
- UNKNOWN 不自动解除意味着需要人工核验路径（管理界面/历史委托查询），
  当前可通过 mx_get_orders 人工核对；P2 的 UNKNOWN 处理界面待做。
- tdx-stock-api 动态技能映射仍未纳入（本地未部署该技能）。
- 真实妙想接口集成、多进程压测、跨日长跑尚未证明（复审清单第
  八节），建议灰度验证。

### 遗留（按用户复审意见）

- 拦截原因统计与监控（P2 阶段）。
- 动态技能的必需工具映射（待部署环境确认工具名）。

## 部署提醒

- 改动在本地仓库，推送 main 会发布 latest 与 SHA 标签镜像；**建议用
  SHA 标签镜像部署**而非仅重启容器。
- 部署后核对：新镜像包含 trade_intents 表（启动日志应有建表/恢复记录）、
  启动日志显示遗留意图恢复、旧 UNKNOWN 意图未被错误清理、前端显示
  执行报告卡片、运行版本与测试版本一致。
- 推送后生效的行为变化：模型只在文字里写"买入/卖出/新增自选/移除自选"
  **不会再被自动执行**——会触发 followup 让模型自己补调工具或改口；
  同标的有未完成买单时买入会被预检拒绝（需先撤单或等成交）；拟交易
  标的未检索记忆会被数据门禁拦截。
- **复审结论采纳：恢复自动交易前先以有监督模拟交易灰度验证**（真实
  委托可见性/延迟、状态同步、跨日恢复），暂不无人监督运行。
