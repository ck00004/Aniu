# mx_core 官方规范更新报告

- 日期：2026-10-16
- 依据：东方财富妙想 Skills 安装指南（https://marketing.dfcfw.com/res/download/A620260623NIYC2U.md）及其引用的 6 个官方技能包（mx-data / mx-search / mx-xuangu / mx-zixuan / mx-moni / mx-poster，均已下载解压并逐行比对）
- 验证：后端 pytest **318 通过**（302 + 16 新增专项回归测试）

---

## 官方包比对结论

分支 mx_core 的**端点与请求参数与官方完全一致**（`/api/claw/query`、`news-search`、`stock-screen`、`self-select/*`、`mockTrading/*`，base `https://mkapi2.dfcfs.com/finskillshub`）。真正的差异在**数据语义层**，本次更新 4 项：

## 更新内容

### 1. 限价单价格放大（交易正确性修复）
- 官方 mx-moni 参考实现明确：**接口要求整数按放大后的价格传入**——沪市（6/9 开头）价格小数位不超过 2 位、深市（0/3 开头）不超过 3 位，`price = 原价 × 10^小数位`（如 600519 限价 1780.5 → 传 178050）。
- 分支此前发送原始浮点数。响应侧价格本就是放大整数格式（`price=185000, priceDec=2`），请求侧对称放大与官方实现一致。
- 实现：`client.py` `trade()`；市价单不受影响（无 price 字段）。

### 2. status 字段业务错误检测（补齐 A5 的盲区）
- 官方 mx-data/mx-search/mx-xuangu/mx-zixuan 全部以**顶层 `status != 0`** 表示业务失败（message 携带原因）；模拟交易类接口用响应体 `code`（成功 0/200，失败如 "501 当前时间不可交易"）。
- 分支的 A5 修复只检测 `code` 字段——数据查询类接口的错误（status 路径）完全漏检。现已扩展 `_raise_on_business_error` 同时检测 `status`（int 与数字字符串）。

### 3. mx_query_market 结果格式化（token 效率大幅提升）
- 移植官方 mx-data 的 `table_to_rows` + `format_terminal`：把 `data.data.searchDataResultDTO.dataTableDTOList` 的深层原始 JSON（headName 时间列 + 指标键值数组 + nameMap 列名映射 + indicatorOrder 列序）格式化为**紧凑的 Markdown 表格**（每表预览前 20 行 + 截断标记）、证券主体列表、查询条件与 questionId。
- 此前原始 JSON 直接交给模型：体积大、结构深、难以解析。格式化后模型可读性与 token 效率显著改善；空结果返回明确错误（"无 dataTableDTOList，请放宽查询条件"）。

### 4. 错误提示与文档
- `ERROR_HINTS` 新增 **code=114**（API密钥不存在/已失效）与**"不可交易"**（code=501 非交易时段）引导。
- `SKILL.md` 重写：委托价格规则（市场识别 + 小数位 + 放大说明）、金额/价格单位说明、数据查询限制（官方警告：大范围查询会导致上下文爆炸）、完整异常处理表（114/113/404/No dataTable/501/网络）、status 与 code 的错误语义说明。

## 明确决策与未采纳项

| 项 | 决策 | 理由 |
|---|---|---|
| **金额单位** | 保留"元"（不做 /1000） | 官方包内部矛盾：SKILL.md 字段表明确标注"单位元"且 `currencyUnit: 1=元`，但参考代码注释称"单位是厘"并除以 1000。字段表 + 响应示例（带小数）+ 分支自 v0.3.6 以来的生产行为三方一致，代码注释更可能是陈旧表述。若实际部署发现金额偏大 1000 倍，改一行即可（已在此记录） |
| **newPost 社区发帖**（官方 mx-moni 新端点） | 不接入 | 自主交易智能体向公共社区自动发帖存在不可控的外部发布风险；如需人工发帖可后续按需添加 |
| **mx-poster AI 社区**（浏览/点赞/回帖） | 不接入 | 与交易分析平台定位无关 |

## 测试（16 个新增回归测试）

- 限价放大：沪市 6/9 开头 2 位、深市 0/3 开头 3 位、市价无 price、超位四舍五入
- status 检测：0 不抛、非零抛（int/字符串）、无 status 走 code 路径（501）
- code=114 引导提示可达
- 结果格式化：官方结构响应 → Markdown 表格（列名/行数/实体/条件/questionId）、30 行截断到 20、缺表报错、非法输入、工具层集成（原始深层结构不再暴露给模型）、空结果错误
- 顺带修复一个既有测试的竞态（梦境测试的 `_wait_for` 断言列表先于 DB 写入）

## 变更文件

修改（3）：`skills/mx_core/client.py`（价格放大 + status 检测）、`skills/mx_core/execution.py`（ERROR_HINTS + 格式化器 + 工具接线）、`skills/mx_core/SKILL.md`（重写）

新增（1）：`tests/test_mx_core_update.py`（16 个用例）
