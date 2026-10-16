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

提供基于东方财富妙想 OpenAPI（`mkapi2.dfcfs.com/finskillshub`）的数据查询与 A 股模拟盘操作工具。

## 工具总览

- `mx_query_market`：权威行情 / 财务 / 关系类结构化数据查询（返回已格式化的 Markdown 表格，含证券主体与查询条件）
- `mx_search_news`：金融资讯、研报、公告、政策检索
- `mx_screen_stocks`：自然语言选股
- `mx_get_positions` / `mx_get_balance` / `mx_get_orders`：模拟组合持仓、资金、委托
- `mx_get_self_selects` / `mx_manage_self_select`：自选股读取与维护
- `mx_moni_trade` / `mx_moni_cancel`：A 股模拟交易下单与撤单

## 使用建议

- 数量必须是 100 的整数倍；LIMIT 委托必须附带有效价格。
- 撤单优先按委托编号，撤单前建议先 `mx_get_orders` 查到最新 order_id。

## 委托价格规则（官方 mx-moni 规范）

- 股票代码仅支持 A 股 6 位数字（如 `600519`、`000001`），系统自动识别市场号。
- 沪市（6/9 开头）价格小数位不超过 2 位；深市（0/3 开头）不超过 3 位。
- `useMarketPrice=true` 时以行情最新价委托，忽略价格参数。
- 金额字段单位为元；价格类字段按 `priceDec` 小数位放大为整数（如 `price=185000, priceDec=2` 表示 1850.00 元）。

## 数据查询限制

请谨慎查询大数据范围的数据（如某只股票 3 年的每日最新价），可能导致返回内容过多、模型上下文爆炸。应缩小查询时间范围、减少返回数据量。

## 异常情形与处理

| 异常 | 原因 | 处理 |
|---|---|---|
| 401 / code=114 / API密钥不存在 | API Key 错误或已失效 | 前往妙想 Skills 页面（https://dl.dfcfs.com/m/itc4）重新获取 |
| code=113 / 今日调用次数已达上限 | 当日调用次数超限 | 前往妙想 Skills 页面获取更多调用次数 |
| 未绑定模拟组合账户 / code=404 | 账户未绑定模拟组合 | 前往妙想 Skills 页面创建模拟账户并绑定组合 |
| No dataTable found / 数据结果为空 | 查询条件太严格或内容不支持 | 放宽查询条件，或到东方财富妙想 AI 页面确认查询方式 |
| 不可交易（如 code=501 当前时间不可交易） | 非连续竞价时段 | 在 A 股交易时段（9:30-11:30、13:00-15:00）重试 |
| connect: Connection refused | 网络无法访问妙想接口 | 检查网络配置 |

数据查询类接口以顶层 `status != 0` 表示业务失败；模拟交易类接口以响应体 `code`（成功为 0/200，失败如 501）表示。
