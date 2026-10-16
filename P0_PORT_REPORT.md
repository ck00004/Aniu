# Aniu-main P0 功能移植报告

- 移植日期：2026-10-16
- 来源：原版 Aniu v1.0.3（`原版项目/Aniu`，github.com/AnacondaKC/Aniu）
- 范围：差异分析（UPSTREAM_COMPARISON.md）P0 清单全部 7 项
- 验证：后端 pytest **266 通过**（225 原有 + 41 新增）；前端 node:test **51 通过**；`vue-tsc` 通过；`vite build` 通过；全部新模块导入检查通过

---

## 移植清单

### P0-6 token 估算修正
- 来源：原版 `llm/context.py:32-39`
- 实现：`token_estimator.py` — ASCII 约 4 字符/token，非 ASCII（中文）全额计入。旧算法对中文低估 2.5-4 倍，导致压缩触发系统性偏晚。
- 测试：中文全额/ASCII 四分之一/混合内容/消息级估算（4 个用例）。

### P0-3 LLM 错误分类 + 重试
- 来源：原版 `llm/errors.py`（错误分类、Retry-After 解析、x-should-retry 覆盖）
- 实现：`llm_service.py` —
  - `LLMErrorCode` 十类分类（authentication/rate_limit/timeout/network/provider_4xx/provider_5xx/empty_response/context_overflow/…）
  - 状态码分类 + 错误文本语义标记（rate_limit_error/overloaded/context_length_exceeded 等）
  - **可重试判定修正**：旧实现只认 `status>=500`，429 限频与超时/网络错误（瞬态错误最常见两类）现在都可重试；上下文溢出明确不重试（重试必然再溢出）
  - Retry-After 三种格式解析（`retry-after-ms` 头、秒数、HTTP 日期）+ `x-should-retry` 覆盖；重试等待优先遵循 Retry-After（上限 30 秒）
  - 超时/网络异常带分类抛出
- 测试：状态分类/语义标记/可重试矩阵/覆盖头/三种 Retry-After 格式/HTTP 错误附带 retry_after（8 个用例）。

### P0-4 usage 归一化累计
- 来源：原版 `llm/usage.py` 的归一化思路
- 实现：
  - run 路径：`aniu_service._extract_usage` 从"取最后一个含 usage 的响应"改为**跨轮求和**（多轮工具调用每轮 prompt 都在增长，真实消耗是各轮之和）
  - 聊天路径：`llm_service.accumulate_usage` 静态方法 + `chat_session_service` 把累计 usage 存入助手消息 `meta_payload.usage`（此前聊天路径完全不记录 token 消耗）
- 测试：跨响应求和/缺失 total 兜底/空输入/run 路径求和/聊天落库（5 个用例）。

### P0-5 AbortSignal + 中止端点 + 前端停止按钮
- 来源：原版 `llm/abort.py` + `business/runs/abort.py` + `abort_registry.py` + `POST /runs/{id}/abort`
- 实现：
  - 新增 `app/services/run_abort_service.py`：`RunAbortRegistry`（run_id → threading.Event，线程安全注册/置位/注销）
  - `run_agent_with_messages` 接受 `cancel_event` 并传入 agent 循环（迭代开始、每个工具调用前、流式读取、重试等待处均有检查点）
  - `execute_run`/`start_run_async` 注册中止事件，`_run_body` 在新闻预取后、agent 循环后、**真实交易执行前**检查中止；`execution_runner.execute_plan` 每个动作执行前检查（中止请求优先于后续真实交易）
  - `_run_body` 专门处理 `LLMStreamCancelled`：run 标记 failed、错误信息"运行已被用户中止。"、调度 next_run_at 正常推进
  - API：`POST /api/aniu/runs/{run_id}/abort`（404 不存在/409 已结束/200 成功；running 但无活动事件的孤儿直接收敛为失败）
  - 前端：TasksView 运行中显示"停止运行"按钮（`api.abortRun`）
- 测试：注册表生命周期/端点 404/409/孤儿收敛/`_run_body` 感知中止并标记 failed（5 个用例）。

### P0-1 交易预检
- 来源：原版 `mx_agent_tools.py:314-389`（`_preflight_trade`/`_preflight_cancel`）
- 实现：`skills/mx_core/execution.py` —
  - **买入（限价）**：可用资金 ≥ 价格×数量，不足时中文报错
  - **卖出**：可卖数量 = 持仓（优先 `availCount`，否则持仓数量 − 在途卖出委托占用推算）；在途卖出委托（已报/未成交/处理中状态）占用份额；状态未知且有剩余时拒绝（"无法确认可卖数量"）
  - **撤单**：委托存在性、代码一致性、可撤状态（已成交/已撤不可撤）
  - 字段解析兼容多命名（stockCode/secCode/code、orderDrt/bsFlag/side、orderStatus/dbStatus 等）
- 测试：资金不足/资金充足/无持仓/超可卖/在途占用/撤单未找到/撤单已完成/撤单可撤（8 个用例）。

### P0-2 交易时段写门禁
- 来源：原版 `agent_runner.py:459-471`（`requires_market_open` + authorize）
- 实现：`skills/mx_core/execution.py` `is_market_open()` — 上海时区 + 交易日历 + 连续竞价时段（9:30-11:30、13:00-15:00）；日历不可用时退化为工作日启发式（告警）。`_handle_moni_trade`/`_handle_moni_cancel` 在真实执行前检查，非交易时段直接阻止并返回中文原因给模型（"非交易时段，交易和撤单写操作已被阻止；仍可继续分析。"）。
- 测试：早盘/午盘开市、午休/开盘前/收盘后/周末闭市、交易工具被拦截（7 个用例）。

### P0-7 工具调用日志
- 来源：原版 `business/stock_api_logs/` + `stock_api_call_logs` 表
- 实现：
  - 新表 `tool_invocation_logs`（provider/operation/请求摘要/状态/状态码/耗时/响应规模/错误摘要，create_all 自动建表）
  - 新服务 `tool_invocation_log_service.py`：`record_invocation`（写入失败只记日志不影响主路径）+ `instrument_invocation` 上下文管理器（计时+成功/失败落库）+ `list_logs`/`summarize` 查询
  - 钩子：`MXClient._post` 每次妙想接口调用自动记录（含业务错误抛出的失败记录）
  - API：`GET /api/aniu/tool-invocation-logs?limit&offset&status`（含汇总统计，需认证）
- 测试：记录与查询/汇总/失败记录/上下文管理器/API 端点含 401（4 个用例）。

---

## 与既有加固的协同

- 中止检查点插在**真实交易执行前**（`_run_body` 执行阶段入口 + runner 每动作前），与重复委托加固（草稿去重/状态过滤/重入防护）形成互补：中止优先于下单。
- 交易预检在 MX 客户端业务错误检测（A5 修复）之前拦截本地可判定的废单（资金不足/超卖/撤已完成单），减少远端往返与废单率。
- usage 累计让 token 统计（runtime overview 的 token 用量）从"只记最后一轮"变为真实总量。

## 测试汇总

| 套件 | 结果 |
|---|---|
| 后端 pytest（原有 225 + 新增 41） | **266 通过 / 0 失败** |
| 前端 node:test | **51 通过 / 0 失败** |
| vue-tsc 类型检查 | 通过 |
| vite build 生产构建 | 通过 |
| 新模块导入检查 | 通过 |

新增测试文件：`backend/tests/test_p0_port.py`（41 个用例，覆盖全部 7 项移植）。

## 变更文件

修改（10）：`app/api/router.py`、`app/db/models.py`、`app/services/{aniu_service,chat_session_service,llm_service,execution_runner_service,token_estimator}.py`、`skills/mx_core/{client,execution}.py`、`frontend/src/{services/api.ts,views/TasksView.vue}`

新增（2）：`app/services/run_abort_service.py`、`app/services/tool_invocation_log_service.py`、`tests/test_p0_port.py`

## 未移植（后续梯队，见 UPSTREAM_COMPARISON.md）

P1：长期记忆系统、token 预算压缩 kernel、SSE 断线续传、每日运行报告。
P2：记忆梦境、公开行情层（query_kline 等）、多渠道模型、行情总览页、CI/Docker 加固。
