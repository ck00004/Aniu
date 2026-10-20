# 记忆梦境"无运行报告"事故排查报告（2026-10-20）

## 现象

用户报告：生产环境 2026-10-19 的记忆梦境整理结果为"无运行报告、无交易结论与证据可供归纳"，但当日明确有交易执行。

## 排查过程（远程只读，未改动远程数据库）

### 1. 远程数据取证

- `memory_dreams` 表：10-15 ~ 10-19 共 5 条梦境记录，全部 `completed`。10-19 的梦境于上海时间 10-20 00:30 正常触发，目标日期正确。
- **10-16、10-17、10-18、10-19 连续四天的梦境结果全部报告同样现象**：`dream_read_reports` 与 `memory_list` 返回 `ok: true, result: null`。10-16 当天有 7 笔成交（含 603986、600276、688825 等），10-19 当天有 4 笔成交（买入 600030/600027、卖出 600938）——数据明明存在。
- `memory_items` 表为空：四天梦境从未写入过任何记忆（因为模型始终认为"无素材"）。
- 上海日 10-19 的 `strategy_runs` 有 14 条记录（UTC 窗口 10-18T16:00 ~ 10-19T16:00），交易运行 8 条（5 成功 3 失败），与梦境报告矛盾。

### 2. 代码比对

远程部署的 `memory_dream_service.py`、`daily_report_service.py`、`memory_service.py`、`llm_service.py` 与本地**逐字节一致**（仅行尾换行差异）。排除"部署版本不一致"。

### 3. 根因定位

`llm_service._slim_tool_result`（工具结果传给模型前的统一出口）按 mx 技能的规范形状做投影：

```python
return {
    "ok": tool_result.get("ok"),
    "tool_name": tool_result.get("tool_name"),
    "summary": tool_result.get("summary"),
    "result": tool_result.get("result"),
}
```

- mx 技能与 builtin_utils 返回 `{ok, tool_name, summary, result}` —— 兼容。
- **梦境/记忆工具返回自有形状**：`dream_read_reports` → `{ok, target_date, offset, limit, total, has_more, runs}`；`memory_list` → `{ok, ..., items}`；`memory_read` → `{ok, items, result_count, keywords}`；`memory_write` → `{ok, action, item}`。
- 这些形状没有 `result`/`summary` 键，投影后模型实际收到：

```json
{"ok": true, "tool_name": null, "summary": null, "result": null}
```

与四天梦境结果里模型的原话"`ok: true`，`result: null`"完全吻合。**数据在数据库里、执行器也正确返回了，但在送入模型前的最后一环被剥掉。**

影响面不止梦境：`skills/memory`（memory_read/memory_write）对所有 run_type 可用，意味着**生产上所有分析/交易/聊天智能体调用记忆工具时看到的都是 `result: null`**——整个记忆系统在智能体侧一直不可见。此外错误形状 `{ok: false, error}` 的 `error` 键也被剥掉，模型连失败原因都看不到。

## 修复（本地，待推送）

`backend/app/services/llm_service.py` 的 `_slim_tool_result`：

- 含 `result` 或 `summary` 键（mx/builtin_utils 规范形状）→ 照旧按元数据精简（行为不变），但额外保留 `error` 键；
- 其他形状（梦境/记忆/错误）→ **原样透传**。

## 验证

1. **单元 + 端到端测试**（新增 `backend/tests/test_tool_result_visibility.py`，7 个）：
   - 规范形状仍被精简、`error` 保留；
   - 梦境报告形状（runs）、记忆形状（items/action/item）、错误形状原样透传；
   - 端到端：真实 `_agent_loop` + 假 LLM 流，模型调用 `dream_read_reports` 后第二轮请求的 tool 消息包含当日运行结论与交易（事故回归测试）；
   - 端到端：`memory_read` 检索结果到达模型（交易路径记忆可见性回归）。
2. **真实生产数据验证**：从远程只读导出 10-16/10-19 两天的运行与委托记录（不含大载荷），本地重建后用修复后代码跑完整链路：
   - 10-16：执行器返回 19 次运行（含 603986/600276/688825/601898/600938 等全部真实交易），模型可见 19 条，无 `result: null`；
   - 10-19：执行器返回 14 次运行（买入 600030 x1000、买入 600027 x4000、卖出 600938 x500×2），模型可见 14 条，包含交易标的。
3. **全量回归**：后端 340 passed（333 + 新增 7）。

## 部署后建议

1. 推送本次修复并重启服务。
2. 补整理历史梦境（已完成/失败的可重跑，会重置为 pending）。**前端已内置入口**：
   - 记忆梦境页 →「梦境记录」标签 → 顶部"补整理指定日期"工具栏：选择历史日期（限昨天及更早）后点击"补整理"；
   - 或展开任意梦境记录，点击"重新整理"按该记录的日期重跑（有覆盖确认）。
   - 等价 API：`POST /api/aniu/memory-dreams/run?date=2026-10-16`。
   - 建议补整理 2026-10-16 与 2026-10-19（10-17/10-18 为周末，仅有非交易日分析，可按需跳过）。
3. 之后每个交易日的梦境应能正常归纳当日交易结论并沉淀长期记忆；`memory_items` 将开始产生数据。

### 前端补整理功能说明（本次新增）

- `useMemories.triggerDream(targetDate?)` 支持指定日期；`backfillDream()` 负责校验（仅允许昨天及更早——当天数据未完整，且会占用当晚自动整理：调度器发现当日已有梦境记录时会跳过触发）。
- UI：梦境记录标签顶部工具栏（日期选择器 `max` 限制为昨天 + 补整理按钮 + 说明文案）；每条梦境详情内"重新整理"按钮（重跑前确认覆盖）。
- 测试：`tests/useMemories.test.ts` 新增 4 个（日期透传、空日期拒绝、今天/未来拒绝、历史日期成功并清空选择）；`tests/memories-view.test.ts` 新增 2 个（工具栏与重跑入口的视图断言）。前端 80 passed，vue-tsc / build 通过，浏览器端到端验收通过（当天日期被拒、历史日期补整理全链路、单条重跑确认）。

## 遗留观察

- 梦境分页单页上限 20 条运行、每条 final_answer 截断 8000 字符、证据有预算，单页工具消息约数十 KB，在上下文管理（溢出恢复）能力范围内。
- 本次事故的测试盲区：既有测试只断言执行器返回值，未断言"模型实际收到的工具消息内容"——新增的端到端测试已补上该层。
