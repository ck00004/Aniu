# Aniu-main 逻辑审计修复报告

- 修复日期：2026-10-16（第二轮：重复委托专项加固）
- 范围：审计报告（LOGIC_AUDIT_REPORT.md）中的全部 18 项 P1 + 同区域 5 项 P2 + 重复委托专项 4 项加固
- 验证：后端 pytest **225 通过**（189 原有 + 36 新增回归测试）；前端 node:test **51 通过**（45 原有 + 6 新增）；`vue-tsc` 类型检查通过；`vite build` 生产构建通过

---

## 〇、第二轮：重复委托专项加固（2026-10-16 追加）

**背景**：生产环境（修复前版本）实际观察到同账户重复委托被执行、敞口被动放大。排查全部交易执行路径后，在第一轮修复（A1/A2/A3 已消除 autofill 重复补单与 followup 绕过管线两条主路径）之外，又发现并修复 4 条残留路径：

| # | 路径 | 修复 |
|---|---|---|
| D-1 | **同 run 内草稿无去重**：主循环草稿 + 一致性 followup 草稿（或模型多轮重复发起）对同一标的生成完全相同的计划 → 双重真实委托。这是"敞口被动放大"的最直接机制 | `_extract_planned_actions` / `_extract_planned_action_drafts` 按幂等键去重（BUY/SELL 五元组、CANCEL 按 order_id、自选股按 query），保留第一条并告警；数量/价格不同的合法加仓不受影响（`aniu_service.py` `_planned_action_dedupe_key`） |
| D-2 | **execute_plan 重放**：动作查询无状态过滤，对同一 run 二次调用会把已 completed 的 BUY/SELL 再次真实执行 | `_list_action_ids` 只取 `planned/failed_retryable`；`_mark_action_executing` 增加重入防护（非可执行状态直接拒绝）（`execution_runner_service.py`） |
| D-3 | **重启后调度立即重触发**：run 被杀时 `next_run_at` 未推进，僵尸 run 虽被标记 failed，但调度仍指向过去 → 重启后立即重跑同一槽位，模型不知道被杀 run 已执行的交易（会话未持久化）→ 重新下单 | `fail_stale_running_runs` 同时把受影响调度的 `next_run_at` 推进到下一 cron 槽位并重置 retry 状态；计算失败退到 +30 分钟（宁推迟勿重触发） |
| D-4 | **成功路径的 next_run_at 计算可抛异常**：交易已执行、run 已标 completed 后，日历拉取失败等异常会把 run 翻转成 failed → 调度重试 → 重跑 → 重复交易；失败路径同类异常还会让 run 卡 running、next_run_at 不推进 → 重启后反复重触发 | 新增 `_safe_compute_next_run_at`：run 收尾路径（成功/失败）的 next_run_at 计算绝不允许抛异常，失败退到 +30 分钟 |

**修复后的交易执行面**（全部收敛到单一管线）：
主循环 / autofill / followup 三条 LLM 路径 → planning executor 生成草稿 → **草稿去重** → execution_runner（**状态过滤 + 重入防护**）→ 真实执行 → 落账。调度侧的重触发路径（重启/失败重试/成功翻转）全部关闭。聊天路径的直接交易为用户主动发起，属设计行为。

**本轮新增测试（5 个，含于 36 个新增之中）**：同 run 重复草稿去重、合法加仓不去重、execute_plan 跳过 completed 动作且二次调用零执行、僵尸 run 收敛时推进调度 next_run_at、日历失败时安全回退。

---

## 一、修复清单

### A. 交易执行链路（P1 × 5）

| 编号 | 修复内容 | 位置 |
|---|---|---|
| A1 | `_enforce_trade_consistency` 增加 run_type 门控：分析任务不再触发交易 autofill（对照自选股版本补齐对称门控） | `aniu_service.py:4582-4593` |
| A2 | autofill 增加名称级查重（与 gap 判定同粒度）：模型已执行过同向操作的标的不再自动补单，消除重复真实委托 | `aniu_service.py` `_analyze_trade_consistency` |
| A3 | 提取 `_build_planning_tool_executor`，两个 enforce 函数的 followup LLM 调用改用计划执行器：followup 中的交易/自选股工具只生成计划，经 execution_runner 统一执行并落账 | `aniu_service.py` |
| A4 | 自选股声称提取改为位置感知：列表项只取冒号前的名称段；裸名称限纯中文 2-4 字（含字母混合名放宽到 8 字）；扩充停用词；sentence_pattern 的 `\s*` 改为 `[ \t]*` 不再跨行吞掉列表项 | `aniu_service.py` `_extract_stock_mentions` / `_extract_claimed_self_select_changes` |
| A5 | `MXClient._post` 增加响应体业务错误检测（数字 code 非 0/200、已知错误标记），异常消息包含响应体文本使 `ERROR_HINTS` 引导真正可达 | `mx_core/client.py` |

### B. 时区（P1 × 2）

| 编号 | 修复内容 | 位置 |
|---|---|---|
| B1 | `list_runs` 日期过滤：上海日界先换算成 UTC 再与 naive UTC 存储值比较（同文件 4762 行已有正确写法，现已对齐） | `aniu_service.py:551-562` |
| B2 | CLS 快讯时间渲染固定 `Asia/Shanghai`（原用服务器本地时区，UTC 容器下偏移 8 小时） | `cls_news_service.py:247` |

### C. 上下文与压缩（P1 × 4）

| 编号 | 修复内容 | 位置 |
|---|---|---|
| C1 | 压缩摘要注入提示词：`_build_persistent_session_prompt_messages` 前插 `[上下文压缩摘要]` system 消息（原 `del session`，压缩等于删除上下文） | `aniu_service.py:5351-5369` |
| C2 | 用户聊天历史裁剪：只取最近 40 条记录（`MAX_HISTORY_RECORDS`），长会话不再必然溢出 | `chat_session_service.py` `stream_chat` |
| C3 | 历史附件降级：仅最近一条带附件的用户消息保留完整附件内容，更早的降级为文件名占位符（消除每轮重复读盘/解析/base64） | `chat_session_service.py` `_build_history_messages` / `_build_user_message_content` |
| C4 | 流式聚合语义：`committed_content`（权威完整文本）与 `delta_buffer`（当前段增量）分离；`final_started` 重置增量缓冲（重试重播不重复累积）；失败/中断时两段以空行分隔而非无分隔拼接 | `chat_session_service.py` `stream_chat` |

### D. 启动单点（P1 × 2）

| 编号 | 修复内容 | 位置 |
|---|---|---|
| D1 | 技能 loader 故障隔离：`_scan_dir` 对每个技能整体 try/except（覆盖 GBK 编码、frontmatter 非法、handler `__init__` 抛异常），单个坏技能不再让整个后端无法启动 | `skills/loader.py` `_scan_dir` / `_load_skill_package` |
| D2 | 日历降级三件套：lifespan 预热 try/except（失败仅告警不阻止启动）；重试间加 2 秒退避；失败负缓存 5 分钟（调度循环不再每 15 秒发起最长 120 秒的阻塞重试）；预热日期改用上海时区 | `main.py` / `trading_calendar_service.py` |

### E. 统计与契约（P1 × 3）

| 编号 | 修复内容 | 位置 |
|---|---|---|
| E1 | runtime overview 改为按 7 天时间窗口 SQL 查询（上限 1000 条），"近3天/近7天"统计不再被"最近 100 条"截断 | `aniu_service.py` `get_runtime_overview` |
| E2 | `update_settings` 改用 `model_dump(exclude_unset=True)`：前端未提交的 automation_* 字段不再被 schema 默认值静默重置 | `aniu_service.py:463-481` |
| E3 | 事件流终止三重保障：① 终端事件（completed/failed）豁免积压丢弃；② 启动时 `fail_stale_running_runs` 把僵尸 running 记录收敛为 failed；③ `/runs/{id}/events` 先校验 run 存在性（不存在返回 404，不再产生无限心跳流） | `event_bus.py` / `main.py` / `router.py` / `aniu_service.py` |

### F. 前端竞态（P1 × 2）

| 编号 | 修复内容 | 位置 |
|---|---|---|
| F1 | 聊天流式定位：事件只写入发起请求时捕获的 assistant 对象且仅当其仍在当前消息列表中（删除 `getStreamingAssistant` 的"取数组末尾"逻辑）；回滚改为按引用过滤（不再误删切换后新会话的消息）；流建立后中断不回滚（与后端持久化状态一致）；流期间切走又切回时完成后自动重载会话 | `useChatSession.ts` |
| F2 | 今日卡片钉死：`liveVisible` 改为 `liveFocused && liveActive`（completed/failed/error 后快照让位给完整详情）；`handleTodayRunSelect` 用 `liveActive` 判断；`reconcileFinishedRun` 结束后解除实时聚焦并选中该运行（用户已手动选择其他运行时不覆盖） | `useRunStream.ts` / `TasksView.vue` |

### 同区域顺带修复的 P2（5 项）

| 编号 | 修复内容 |
|---|---|
| P2-21 | 条件词"如"不再误杀"如下"句式（`_line_has_conditional_marker`，两处提取器统一使用） |
| P2-22 | CANCEL 声称/实际比对：实际撤单读取 `stock_code` 字段；仅带 order_id 的撤单也产生记录；存在实际撤单即视为一致（不再对已执行撤单追加"未实际撤单"错误警告） |
| P2-36 | 聊天流中断时本地回滚与后端持久化状态分叉（并入 F1 修复：流建立后不回滚） |
| P3（`@/` 别名） | `useRunStream.ts` 改为相对导入，node 测试运行器可解析（也是新增测试的前提） |
| 测试锁定行为更新 | 两个锁定旧缺陷行为的测试改为锁定新行为：压缩摘要注入（test_automation_session）、启动容忍日历失败（test_chat_api） |

---

## 二、测试验证

**后端（pytest，225 通过 / 0 失败）**
- 原有 189 个测试全部通过（其中 2 个锁定旧缺陷行为的测试已更新为锁定修复后行为）
- 新增 `backend/tests/test_logic_fixes.py` 36 个回归测试，覆盖：A1 门控（分析跳过/交易执行）、A2 名称级查重、A4 提取（行内理由/长短语/正常名称/列表项）、A5 响应体检测（数字 code/标记/纯文本/成功体/hint 匹配）、B1 上海日界（凌晨 0:30 与上午 10:00 归属）、B2 上海时间渲染、C2 历史裁剪、C3 附件降级、C4 分隔拼接/重试重播、D1 三种坏技能隔离、D2 负缓存、E2 部分更新、E1 超 100 条统计、E3 终端事件豁免/404/僵尸收敛、P2-21/22、重复委托专项（D-1 草稿去重/合法加仓、D-2 状态过滤与重放防护、D-3 调度推进、D-4 安全回退）

**前端（node:test，51 通过 / 0 失败）**
- 原有 45 个测试全部通过
- 新增 7 个测试：`useChatSession.test.ts` +3（切换会话不污染、按引用回滚、流中断不回滚）、`useRunStream.test.ts` +4（运行中可见/结束隐藏、失败隐藏、连接错误隐藏）

**其他验证**
- `vue-tsc -b` 类型检查通过
- `vite build` 生产构建通过
- 全部修改的后端模块导入检查通过

---

## 三、未修复项（审计报告中的其余 P2/P3）

按审计报告第四、五节原样保留，建议后续按需处理。其中相对值得优先的：
- P2-33：ScheduleView 保存静默删除非模板任务（需产品决策：加确认弹窗或改增量更新）
- P2-1 剩余部分：调度线程被同步长任务阻塞使 `stop()` 失效（需把 execute_run 移出调度线程，属结构性改动）
- P2-29/30：启动路径迁移逻辑的幂等标记、历史 run 重写（需引入 schema version 表）
- P3 组：死代码清理与 registry/skill_stack 平行实现收敛

## 四、变更文件清单

修改（15）：`backend/app/api/router.py`、`backend/app/main.py`、`backend/app/services/{aniu_service,chat_session_service,cls_news_service,event_bus,trading_calendar_service}.py`、`backend/app/skills/loader.py`、`backend/skills/mx_core/client.py`、`backend/tests/{test_automation_session,test_chat_api}.py`、`frontend/src/composables/{useChatSession,useRunStream}.ts`、`frontend/src/views/TasksView.vue`、`frontend/tests/useChatSession.test.ts`

新增（3）：`backend/tests/test_logic_fixes.py`、`frontend/tests/useRunStream.test.ts`、本报告

未提交 git（当前在 main 分支，如需提交请告知）。
