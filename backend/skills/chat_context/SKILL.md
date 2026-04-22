---
name: chat_context
description: 为 AI 聊天提供账户摘要、持仓、委托和任务运行记录的按需读取工具。
metadata:
  aniu:
    handler_module: skills.chat_context.handler
    run_types: [chat]
    category: chat
---

# 聊天上下文技能（chat_context）

这个技能只在 `chat` 模式下可用。不要在每轮对话开始时无脑读取全部数据；应先判断用户问题是否需要账户、持仓、委托或历史任务信息，再按需调用最小必要工具。

## 工具

- `chat_get_account_summary`：读取账户总览摘要，适合问收益、仓位、资金、账户状态。
- `chat_get_positions`：读取当前持仓明细，适合问单票盈亏、仓位结构、可卖数量。
- `chat_get_orders`：读取当前委托明细，适合问挂单状态、成交情况、撤单前确认。
- `chat_list_runs`：读取历史任务列表，适合先定位最近或指定日期的任务。
- `chat_get_run_detail`：按 `run_id` 读取单条任务的输出内容、工具调用摘要和交易记录。

## 使用建议

1. 用户只做泛化问题时，不必先取数据。
2. 用户问到账户、持仓、委托时，优先读取对应工具，不要猜。
3. 用户问到“最近一次任务”“某次运行为什么这么做”“历史任务内容”时，先 `chat_list_runs` 再 `chat_get_run_detail`。
4. 控制调用量，避免一次读取不必要的大量历史任务内容。
