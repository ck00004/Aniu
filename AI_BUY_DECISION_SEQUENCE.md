# Aniu-main AI 买股决策时序图

这份文档描述当前源码中，一次完整的 AI 买股决策是如何发生的。

说明两点：

- 当前系统不是固定规则选股器，而是大模型在工具集约束下自主决定先查什么、筛什么、买什么。
- 下图展示的是典型买股路径。实际运行中，工具调用顺序可能会因为提示词、账户状态、行情和新闻而变化。

---

## 1. 源码级时序图

```mermaid
sequenceDiagram
    autonumber
    actor U as 用户
    participant TV as 前端 TasksView
    participant API as 前端 api.ts
    participant RS as 前端 useRunStream
    participant Router as FastAPI router
    participant Aniu as aniu_service
    participant DB as SQLite
    participant Bus as EventBus
    participant Worker as 异步运行线程
    participant Session as 自动化会话上下文
    participant LLM as llm_service
    participant Model as 大模型 API
    participant Registry as skill_registry
    participant MXSkill as mx_execution_service
    participant MX as MXClient 妙想接口

    U->>TV: 点击 执行交易
    TV->>API: runNowStream(scheduleId?, runType=trade?)
    API->>Router: POST /api/aniu/run-stream
    Router->>Aniu: start_run_async(trigger_source=manual, ...)

    Aniu->>Aniu: 获取全局运行锁
    Aniu->>Aniu: _prepare_run(...)
    Aniu->>DB: 创建 StrategyRun(status=running)
    Aniu->>DB: 读取 AppSettings 与 Schedule
    Aniu-->>Router: 返回 run_id
    Router-->>API: { run_id }
    API-->>TV: run_id

    TV->>RS: start(run_id)
    RS->>API: runEventsUrl(run_id)
    API->>Router: GET /api/aniu/runs/{run_id}/events
    Router->>Bus: 订阅该 run_id 的 SSE 事件

    Aniu->>Worker: 启动后台线程执行 _run_body(...)
    Worker->>Bus: publish(stage=started)
    Worker->>MX: 创建 MXClient(api_key)
    Worker->>Bus: publish(stage=llm)

    Worker->>Session: _prepare_persistent_session_context(run_id,...)
    Session->>DB: 获取或创建 automation ChatSession
    Session->>DB: 写入本轮 user 消息
    Session->>DB: 读取近期自动化历史消息
    Session->>Aniu: 返回 prompt messages

    Worker->>LLM: run_agent_with_messages(app_settings, client, messages, emit)
    LLM->>Registry: build_tools(run_type=trade)
    Registry-->>LLM: 返回 trade 模式工具集

    loop 最多 100 轮工具调用
        LLM->>Bus: publish(llm_request)
        LLM->>Model: 调用 chat/completions，携带 messages + tools
        Model-->>LLM: 返回 assistant message 或 tool_calls

        alt 模型要求筛选候选股
            LLM->>Bus: publish(tool_call running: mx_screen_stocks)
            LLM->>Registry: execute_tool(mx_screen_stocks, query)
            Registry->>MXSkill: _handle_screen_stocks
            MXSkill->>MX: screen_stocks(query)
            MX-->>MXSkill: 候选股列表
            MXSkill-->>Registry: tool_result(ok, result)
            Registry-->>LLM: tool_result
            LLM->>Bus: publish(tool_call done: mx_screen_stocks)
        end

        alt 模型要求查行情/财务/板块资金
            LLM->>Bus: publish(tool_call running: mx_query_market)
            LLM->>Registry: execute_tool(mx_query_market, query)
            Registry->>MXSkill: _handle_query_market
            MXSkill->>MX: query_market(query)
            MX-->>MXSkill: 行情/财务/资金/关系数据
            MXSkill-->>Registry: tool_result
            Registry-->>LLM: tool_result
            LLM->>Bus: publish(tool_call done: mx_query_market)
        end

        alt 模型要求查资讯/公告/政策
            LLM->>Bus: publish(tool_call running: mx_search_news)
            LLM->>Registry: execute_tool(mx_search_news, query)
            Registry->>MXSkill: _handle_search_news
            MXSkill->>MX: search_news(query)
            MX-->>MXSkill: 新闻结果
            MXSkill-->>Registry: tool_result
            Registry-->>LLM: tool_result
            LLM->>Bus: publish(tool_call done: mx_search_news)
        end

        alt 模型要求确认账户约束
            LLM->>Registry: execute_tool(mx_get_balance / mx_get_positions / mx_get_orders)
            Registry->>MXSkill: 对应 handle
            MXSkill->>MX: get_balance / get_positions / get_orders
            MX-->>MXSkill: 资金/持仓/委托
            MXSkill-->>LLM: tool_result
        end

        alt 模型决定买入某只股票
            LLM->>Bus: publish(tool_call running: mx_moni_trade)
            LLM->>Registry: execute_tool(mx_moni_trade, {action=BUY, symbol, quantity, ...})
            Registry->>MXSkill: _handle_moni_trade
            MXSkill->>MXSkill: 校验 BUY/SELL, quantity, price_type, price
            MXSkill->>MX: trade(BUY, symbol, quantity, price_type, price)
            MX-->>MXSkill: 模拟下单结果
            MXSkill-->>Registry: tool_result + executed_action
            Registry-->>LLM: tool_result
            LLM->>Bus: publish(tool_call done: mx_moni_trade)
        end

        alt 模型不再调用工具
            Model-->>LLM: 返回最终自然语言结论 final_answer
            LLM->>Bus: publish(final_started/final_delta/final_finished)
            break 结束工具循环
        end
    end

    LLM-->>Worker: final_answer + tool_history + llm payloads
    Worker->>Aniu: _extract_executed_actions(tool_calls)
    Aniu->>Aniu: 从 executed_action 提取 BUY/SELL/CANCEL

    Worker->>DB: 更新 StrategyRun(final_answer, payloads, executed_actions, status=completed)
    Worker->>DB: 为 BUY/SELL 动作写入 TradeOrder
    Worker->>DB: 更新 Schedule(last_run_at, next_run_at, retry_count)
    Worker->>DB: 写入 automation assistant 消息
    Worker->>DB: 必要时压缩自动化会话摘要

    Worker->>Bus: publish(trade_order)
    Worker->>Bus: publish(completed)
    Worker->>MX: close()

    RS->>RS: 接收 SSE，实时展示阶段、接口调用、交易执行、最终结论
    TV->>API: getRun(run_id) / refreshAfterRunCompletion()
    API->>Router: GET /api/aniu/runs/{run_id}
    Router->>Aniu: get_run(run_id)
    Aniu->>DB: 读取 StrategyRun + TradeOrder
    DB-->>Aniu: 完整运行详情
    Aniu-->>TV: 展示本次买股记录和分析结论
```

---

## 2. 这张图里的关键业务含义

### 2.1 买股不是固定规则，而是工具驱动的多轮决策

源码里的买股路径不是：

- 先写死某个策略函数
- 然后直接得出买入代码

而是：

1. 大模型先看到任务目标和历史上下文
2. 再看到 trade 模式下允许使用的工具集
3. 自己决定先筛股、再查行情、再查新闻、再查账户约束
4. 最终决定是否发起 BUY 类型的 mx_moni_trade

所以“选股”和“买股”是同一条 AI 决策链里的两个阶段，不是两个完全分离的模块。

### 2.2 买股前通常会经过四类检查

典型情况下，模型会先做这些检查：

1. 候选股筛选：mx_screen_stocks
2. 行情和基本面确认：mx_query_market
3. 新闻和事件确认：mx_search_news
4. 账户约束确认：mx_get_balance、mx_get_positions、mx_get_orders

也就是说，真正发 BUY 指令之前，模型理论上已经拿到了：

- 候选股票池
- 股票或板块的行情和资金数据
- 新闻催化或风险信息
- 当前账户是否有钱、是否已有仓位、是否存在未完成委托

### 2.3 真正的下单动作在工具执行层被再次校验

即使模型已经决定买入，执行层仍会校验：

- action 是否为 BUY 或 SELL
- symbol 是否为空
- quantity 是否大于 0
- quantity 是否是 100 的整数倍
- LIMIT 单是否有合法价格

因此，模型决定买，不等于系统一定能成功下单。

### 2.4 下单成功后会同时留下四类痕迹

一次成功买股后，结果会沉淀到：

1. StrategyRun：保存整轮分析、工具调用链和最终结论
2. TradeOrder：保存结构化买入动作
3. 自动化会话：保存本轮 user/assistant 记忆
4. SSE 事件流：实时通知前端本轮发生了交易动作

这也是为什么 Tasks 页面既能显示分析结论，也能显示买入动作。

---

## 3. 关键源码定位

前端手动触发交易：

- frontend/src/views/TasksView.vue
- frontend/src/composables/useRunStream.ts
- frontend/src/services/api.ts

后端任务入口：

- backend/app/api/router.py
- backend/app/services/aniu_service.py

LLM 工具循环：

- backend/app/services/llm_service.py

妙想技能与工具定义：

- backend/skills/mx_core/SKILL.md
- backend/skills/mx_core/tool_specs.py
- backend/skills/mx_core/handler.py
- backend/skills/mx_core/execution.py

动作提取与交易落库：

- backend/app/services/aniu_service.py

事件流：

- backend/app/services/event_bus.py

---

## 4. 最关键的一句话

当前源码里，一次完整买股决策不是“后端算出股票再下单”，而是“前端触发 trade 任务后，大模型在自动化上下文中多轮调用选股、行情、资讯和账户工具，最终调用 mx_moni_trade 发起 BUY，并由后端提取 executed_action 落为 TradeOrder 与运行记录”。