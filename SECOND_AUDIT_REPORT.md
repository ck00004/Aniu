# 二次审计报告（2026-10-20）：以"漏检类别"为主线的机械一致性审计

## 审计动机与方法

前次审计（LOGIC_AUDIT_REPORT.md）以业务行为为主线，漏掉了两起后来在生产爆发的问题：
`_slim_tool_result` 剥掉非规范工具载荷（梦境连续空转）、CANCEL 正则 `group(3)` 越界
（11:15 运行崩溃）。本次审计不再按业务功能走查，而是针对**漏检问题的类别**逐类做
机械化核查——每一条都枚举全部出现点并逐一比对，不依赖"读代码时注意到"。

## 审计维度与结果

### 1. 正则分组一致性（后端 + 前端）——通过

枚举后端 `app/`、`skills/` 全部 17 处 `.group(N)`/命名组访问，逐一与其模式定义比对：
`_DOWNLOAD_LINK_RE`、`_FRONTMATTER_RE`、`_DREAM_TIME_PATTERN`、`_SEARCH_RESULT_RE`/
`_SNIPPET_RE`（命名组）、aniu_service 各提取器、mx_core `group(0)/group(1)`——
CANCEL 修复后无遗留越界。全部 `re.sub` 均为字面量替换，无反向引用风险。
前端 6 处带分组正则（useScheduleForm、useAnalysisRuns、formatters、legacy、OverviewView）
的分组访问全部与模式一致。

### 2. 工具返回形状契约——通过（依赖本次修复）

枚举全部工具执行器与消费者：

| 生产者 | 形状 | 消费者兼容性 |
|---|---|---|
| mx 技能、builtin_utils、chat_context、规划器（_plan_*） | 规范 `{ok, tool_name, summary, result}` | `_slim_tool_result` 规范投影 ✓ |
| 记忆技能、梦境 4 工具 | 自有形状 `{ok, runs/items/...}` | 修复后原样透传 ✓ |
| 各错误路径 | `{ok: false, error}` | 修复后 error 可见 ✓ |
| 存储路径（skill_payloads.tool_calls[].result） | 原始 executor 返回 | 日报证据提取 `_tool_result_text` 对任意形状 json.dumps ✓；`_extract_tool_result` 只用于 mx 读工具（规范形状）✓ |

### 3. 事后处理崩溃路径——通过（依赖本次修复），2 条加固建议

LLM 完成后处理模型可控文本的全部函数逐一核查：`_extract_claimed_trade_changes`
（已修复）、`_extract_executable_trade_claims`（int 有 try/except、float 作用于
正则校验过的数字文本）、`_extract_claimed_self_select_changes`（findall 无分组访问）、
`_has_*_consistency_gap` / `_finalize_*` / `_build_analysis_summary`（纯字符串/集合
操作）、`accumulate_usage`（全 isinstance 防护）、`_extract_planned/executed_actions`
（isinstance 逐层防护）、`build_run_error_message`（int 作用于系统计数字段）。

加固建议（当前无实际触发路径，属防御性缺口）：
- **A1** `_extract_action_records` 的 `int(action_payload.get("quantity") or 0)` 无
  try/except。两个生产者（`_plan_trade`、execution_runner）目前都归一化为 int，
  但若未来新增生产者传入非数值，将复现 run 1778 式崩溃。建议包 try/except 归零。
- **A2** agent loop 对模型返回的非法 JSON 工具参数直接 `raise RuntimeError` 失败整次
  运行。报错信息清晰，属可接受的设计取舍；如需更鲁棒可跳过单个坏调用并告知模型。

### 4. 前后端契约（types.ts vs schemas）——兼容，1 条功能缺口

逐字段比对 RunSummary/RunDetail/ScheduleConfig/MemoryItem/MemoryActivity/MemoryDream/
AppSettings/AccountOverview：前端类型均为后端字段子集（后端多出的字段被前端忽略，
无运行时风险）。

- **F1（功能缺口，非 bug）**：后端 `automation_dream_time`（梦境整理时间，默认 00:30）
  在 `AppSettingsRead` 中返回，但前端 `AppSettings` 未声明、设置页未暴露——用户无法
  从 UI 修改梦境时间。已确认**不会**被保存设置静默重置：`update_settings` 使用
  `exclude_unset`，前端 reactive 对象不含该字段故不会回传。同类后端维护字段
  （automation_session_id、target_prompt_tokens 等）为有意设计，但梦境时间属于
  用户可感知配置，建议在设置页暴露。

### 5. 服务间 dict 契约——通过

- `build_daily_report` 键 ↔ 梦境工具 `report.get("runs")` ✓
- `list_memories` `{total, items}` ↔ 梦境工具 ✓
- `list_runs_page` `{items, has_more, next_before_id}` ↔ chat_context 技能 ✓
- `get_account_overview` 全部键（含 daily_profit_trade_date）↔ chat_context 消费
  （空概览缺该键但消费侧用 `.get()`，安全）✓
- `_serialize_run_summary` 直接走 Pydantic schema ✓

## 结论

五个维度全部核查完毕。除本次会话已修复的两起生产事故（`_slim_tool_result`、
CANCEL 正则）外，**未发现新的可触发崩溃、数据丢失或静默重置路径**。遗留事项为
1 条功能缺口（F1）与 2 条防御性加固建议（A1、A2）——**三项均已处置**：

- **F1 已增补**：设置页新增"梦境整理时间"字段（`type="time"`），store 与类型
  同步、保存自动回传；后端 `AppSettingsUpdate` 增加 HH:MM 格式校验（只校验
  写入路径，历史库脏值不影响读取），`update_settings` 往返持久化。
- **A1 已加固**：`_extract_action_records` 的数量解析包 try/except 归零，
  非数值 quantity 不再可能让运行崩溃。
- **A2 已调整**：模型返回非法 JSON 工具参数时不再失败整次运行——错误作为
  工具结果回传给模型（含"请检查参数格式后重新调用"提示），坏调用记入
  tool_history 供追溯，模型可在轮次预算内修正重试。

## 本次审计与上次的方法差异（为什么这次能覆盖住）

1. 每个类别先**枚举全部出现点**（grep 全量），再逐一比对——不依赖阅读时的注意力；
2. 比对对象是**生产者与消费者的机械契约**（分组数、键名、形状），不是业务语义；
3. 对"模型可控输入"单独划为攻击面，逐函数核查无保护解析；
4. 契约检查覆盖前后端与服务间两个方向，而非只看后端内部。

回归状态：后端 347 passed（含两起事故的 10 个回归测试 + F1/A1/A2 的 4 个新测试）；
前端 82 passed，vue-tsc / build 通过（含梦境时间字段的 2 个新测试）。
