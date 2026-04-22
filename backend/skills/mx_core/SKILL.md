---
name: mx_core
description: 东方财富妙想股票行情、资讯、选股与A股模拟交易核心工具集
metadata:
  aniu:
    handler_module: skills.mx_core.handler
    run_types: [analysis, trade, chat]
    category: finance
---

# 妙想核心技能（mx_core）

提供基于东方财富妙想 OpenAPI 的数据查询与 A 股模拟盘操作工具。

## 工具总览

- `mx_query_market`：权威行情 / 财务 / 关系类结构化数据查询
- `mx_search_news`：金融资讯、研报、公告、政策检索
- `mx_screen_stocks`：自然语言选股
- `mx_get_positions` / `mx_get_balance` / `mx_get_orders`：模拟组合持仓、资金、委托
- `mx_get_self_selects` / `mx_manage_self_select`：自选股读取与维护
- `mx_moni_trade` / `mx_moni_cancel`：A 股模拟交易下单与撤单

## 使用建议

- 数量必须是 100 的整数倍；LIMIT 委托必须附带有效价格。
- 撤单优先按委托编号，撤单前建议先 `mx_get_orders` 查到最新 order_id。