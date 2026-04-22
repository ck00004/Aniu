# Aniu-main 开发手册

这份手册面向后续直接修改代码的人，目标不是介绍产品，而是回答下面这些开发问题：

- 功能入口在哪里。
- 一个需求通常要改后端哪层、前端哪层。
- 数据存在哪里，改动会影响哪些模块。
- 哪些地方是高风险点，改之前要先看什么。

---

## 1. 项目定位

Aniu-main 是一个面向 A 股分析与模拟交易的全栈应用，核心由四块组成：

1. FastAPI 后端
2. Vue 3 前端
3. SQLite 本地持久化
4. 动态技能系统与调度系统

从工程视角看，它不是单纯的页面应用，而是一个带后台任务、持久化会话、工具调用链和技能装载能力的平台。

---

## 2. 目录地图

### 2.1 顶层目录

- backend: FastAPI 后端、数据库模型、业务服务、测试
- frontend: Vue 3 前端页面、状态管理、组合式逻辑、接口调用
- data: 运行时数据目录，包括 jwt_secret.txt 和数据库文件
- docs: 展示素材和补充文档
- docker-compose.yml / Dockerfile: 部署入口

### 2.2 后端目录职责

- backend/app/main.py: 应用启动入口和生命周期管理
- backend/app/api: FastAPI 路由层
- backend/app/core: 配置、认证、常量、限流
- backend/app/db: 数据库初始化、迁移兼容、模型定义
- backend/app/schemas: Pydantic 请求和响应模型
- backend/app/services: 核心业务逻辑
- backend/app/skills: 内置技能和技能注册
- backend/tests: 后端测试

### 2.3 前端目录职责

- frontend/src/router: 路由定义和登录守卫
- frontend/src/views: 页面级组件
- frontend/src/components: 复用 UI 组件
- frontend/src/composables: 页面逻辑和接口编排
- frontend/src/services: API 调用层
- frontend/src/stores: Pinia 状态管理
- frontend/src/config: 导航和静态配置
- frontend/src/utils: 工具函数
- frontend/tests: 前端测试

---

## 3. 启动与运行模型

### 3.1 本地开发

后端：

```bash
cd backend
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

前端：

```bash
cd frontend
npm install
npm run dev
```

本地联调规则：

- 前端开发服务器端口为 3003
- Vite 会把 /api 和 /health 代理到 8000
- 所以日常前端开发不需要手工改 API 地址

### 3.2 后端启动时做了什么

backend/app/main.py 在启动时会：

1. 初始化数据库
2. 重新加载技能注册表
3. 从数据库恢复技能启停状态
4. 预热交易日历
5. 启动调度线程
6. 如果存在打包后的前端静态文件，则直接托管前端

结论：

- 任何和应用启动、调度、技能装载、Docker 托管有关的问题，先看 main.py
- 任何和“为什么本地正常、容器里不正常”有关的问题，也先看 main.py

---

## 4. 后端分层

后端基本按这四层组织：

1. router: 对外接口入口
2. schema: 输入输出契约
3. service: 业务流程核心
4. db: 模型与持久化

改代码时尽量遵循这个边界，不要把业务逻辑继续堆进 router。

### 4.1 Router 层

总入口是 backend/app/api/router.py。

已确认的主要接口分类：

- 登录: /login
- 设置: /settings
- 技能管理: /skills
- 调度: /schedule
- 手动运行和流式运行: /run, /run-stream
- 运行记录和事件: /runs, /runs/{id}/events
- 聊天会话和消息
- 上传附件
- 账户总览、运行总览

开发建议：

- 新增接口时，先在 schema 中定义输入输出，再加 service，再暴露到 router
- Router 层尽量只做参数校验、异常转 HTTP 状态码、依赖注入

### 4.2 Schema 层

当前统一放在 backend/app/schemas/aniu.py。

这个文件比较大，但好处是协议集中。可直接分成几组理解：

- AppSettingsRead / AppSettingsUpdate: 设置页契约
- ScheduleRead / ScheduleUpdate: 调度页契约
- RunSummaryRead / RunDetailRead: 任务详情和运行记录契约
- AccountOverviewRead: 总览页账户数据契约
- ChatSessionRead / ChatMessageRead / ChatStreamRequest: 聊天模块契约
- SkillListItemRead / SkillInfoRead: 技能页契约

开发建议：

- 如果前端报字段不存在或类型不匹配，先对照这里
- 后端返回结构变化时，前端 types.ts 也要同步

### 4.3 Service 层

这是项目最关键的层。

#### aniu_service.py

主业务服务，负责：

- 登录认证
- 设置初始化
- 调度列表和调度更新
- 手动运行和异步运行
- 运行记录聚合
- 账户总览与运行总览
- 自动化交易会话相关上下文

如果需求涉及“任务如何执行”“任务记录如何形成”“总览页数据从哪里来”，优先看这里。

#### chat_session_service.py

负责：

- 聊天会话管理
- 消息持久化
- SSE 流式聊天
- 附件上传与读取
- 文本、图片、docx、xlsx、pptx 的附件提取

如果需求涉及“聊天发不出去”“附件无法读取”“SSE 中断”“消息分页”，优先看这里。

#### skill_admin_service.py

负责：

- 技能列表
- 技能启停
- 从 SkillHub / ClawHub / zip 导入技能
- 工作区技能删除
- 技能兼容性信息构建

如果需求涉及“技能导入失败”“技能开关状态不持久”“工作区技能目录结构”，优先看这里。

#### llm_service.py

负责：

- 构造大模型请求
- 拼接系统提示词
- 注入工具定义
- 处理 tool call 循环
- 流式输出和取消
- 上游错误转友好错误

如果需求涉及“模型为什么调用了工具”“聊天/任务的提示词从哪里拼出来”“上游 LLM 报错如何处理”，优先看这里。

#### scheduler_service.py

负责：

- 启动后台调度线程
- 轮询 process_due_schedule

它很薄，但它是所有定时任务的调度触发器。改定时行为时别只改表单和 cron，必须同时检查调度线程和 due 逻辑。

#### 其他关键服务

- event_bus.py: 运行事件和流式推送基建
- mx_service.py: 妙想能力接入
- mx_skill_service.py: 妙想工具与技能桥接
- token_estimator.py: 上下文 token 估算
- trading_calendar_service.py: 交易日历预热和缓存

### 4.4 DB 层

核心文件：backend/app/db/models.py 和 backend/app/db/database.py。

#### 主要数据表

app_settings:

- 大模型配置
- 妙想 API Key
- 系统提示词
- 自动化会话相关配置
- 禁用技能列表

strategy_schedules:

- 调度配置
- run_type
- cron_expression
- task_prompt
- timeout
- enabled

strategy_runs:

- 每次任务运行记录
- 运行状态
- LLM 请求和响应负载
- 工具调用结果
- 决策数据
- 最终输出

trade_orders:

- 任务执行产生的模拟委托记录

chat_sessions:

- 聊天会话元信息
- 摘要版本
- 自动压缩相关字段

chat_messages:

- 每条聊天消息
- 来源、run_id、tool_calls、附件信息

chat_attachments:

- 附件元数据和存储路径

#### 数据库初始化特点

database.py 不只是 create_all，还包含一批 SQLite 兼容迁移逻辑：

- 自动补列
- 自动补索引
- 对旧 run_type 做回填
- 对旧 chat/session 字段做兼容

开发建议：

- 这个项目没有正式迁移框架，数据库演进主要靠 init_db 里的补丁逻辑
- 新增字段时，不仅要改 models，还要改 database.py 中对应的兼容补列逻辑
- 否则老库升级后会直接出问题

---

## 5. 前端分层

前端可以按四层理解：

1. views: 页面
2. composables: 页面逻辑
3. services/api.ts: 请求封装
4. stores/legacy.ts: 全局状态和页面公共数据

### 5.1 路由和页面

frontend/src/router/index.ts 定义了这些页面：

- /login
- /overview
- /tasks
- /chat
- /schedule
- /settings

frontend/src/config/navigation.ts 里定义了头部导航项：

- 总览
- AI分析
- AI聊天
- 定时设置
- 功能设置

### 5.2 API 层

frontend/src/services/api.ts 是前端所有后端通信的总入口，负责：

- token 存储和读取
- 未授权跳转登录
- fetch 超时控制
- 表单和 JSON 请求封装
- 各业务接口函数导出

开发建议：

- 新增后端接口时，前端先补 api.ts，再让 composable 或 view 调用
- 登录态、401 跳转、超时都已经集中在这里，不要在各页面重复写

### 5.3 Store 层

frontend/src/stores/legacy.ts 负责：

- 设置数据
- 调度配置
- 账户总览
- 运行总览
- 运行详情缓存
- 刷新节流与冷却时间

虽然名字叫 legacy，但它实际上还承载了不少主流程数据。改总览页、设置页、调度页时通常都会碰到这个 store。

### 5.4 Composable 层

这些 composable 基本就是“页面逻辑控制器”：

- useAnalysisRuns.ts: AI 分析页数据映射、运行详情展示、token 展示、工具调用展示
- useChatSession.ts: 聊天会话加载、消息分页、SSE 流处理、附件临时队列
- useChatSessions.ts: 会话列表管理
- usePersistentSession.ts: 持久会话相关逻辑
- useRunStream.ts: 运行任务时的流式事件消费
- useScheduleForm.ts: 调度页面表单和 cron 生成
- useSkillManager.ts: 技能列表、导入、启停、删除

开发建议：

- 页面交互问题优先看 composable，不要一上来就改 view
- View 最好保持渲染职责，状态流和请求流尽量放 composable

---

## 6. 页面到后端的映射

### 6.1 登录页

页面：frontend/src/views/LoginView.vue

主要依赖：

- api.login
- router 守卫
- 本地存储的 token 和登录标记

后端入口：/api/aniu/login

如果登录异常，优先检查：

- APP_LOGIN_PASSWORD 是否配置
- token 是否被成功写入 localStorage
- 401 是否被前端 handleUnauthorized 接管

### 6.2 总览页

页面：frontend/src/views/OverviewView.vue

主要依赖：

- store 中的 account
- store 中的 runtimeOverview
- store 中的 activeScheduleCards

后端相关能力：

- 账户总览
- 运行总览
- 调度列表

如果总览数据不对，通常不是页面问题，优先追 store，再追 aniu_service。

### 6.3 AI 分析页

页面：frontend/src/views/TasksView.vue

主要依赖：

- api.runNow / api.runNowStream
- api.listRuns / api.listRunsPage / api.getRun
- useAnalysisRuns
- useRunStream

后端相关能力：

- 手动运行
- 运行事件流
- 运行详情聚合

如果问题是“运行完成但页面没刷新”“工具调用展示不完整”，先查前端 composable 映射，再查 run detail payload。

### 6.4 AI 聊天页

页面：frontend/src/views/ChatView.vue

主要依赖：

- useChatSession
- useChatSessions
- SSE 流接口
- 附件上传接口

后端相关能力：

- chat_session_service
- llm_service
- event_bus 或流式输出

如果问题是“聊天有结果但历史里没保存”，要同时查 SSE 返回和消息持久化逻辑。

### 6.5 定时设置页

页面：frontend/src/views/ScheduleView.vue

主要依赖：

- useScheduleForm
- api.getSchedule
- api.updateSchedule

后端相关能力：

- list_schedules
- replace_schedules
- scheduler_service

这里最容易踩的坑是：

- 前端生成的 cron 对了，但后端没有计算 next_run_at
- schedule 表结构改了，但兼容逻辑没补
- run_type 改动后分析任务和交易任务混了

### 6.6 功能设置页

页面：frontend/src/views/SettingsView.vue

主要依赖：

- api.getSettings / api.updateSettings
- useSkillManager
- store.settings

后端相关能力：

- app_settings 表
- skill_admin_service

如果问题是“设置保存成功但运行时不生效”，要区分：

- 是写库失败
- 是读取配置走了环境变量默认值
- 还是运行时缓存没有刷新

---

## 7. 关键调用链

### 7.1 手动运行一条分析/交易任务

1. 前端点击运行
2. frontend/src/services/api.ts 调用 /run 或 /run-stream
3. router.py 进入 run_once 或 run_stream
4. aniu_service 解析 run_type 和任务配置
5. llm_service 构造请求并驱动工具调用循环
6. 技能系统或妙想工具被调用
7. 运行结果写入 strategy_runs 和 trade_orders
8. 前端再取 run detail 或订阅事件流刷新展示

### 7.2 聊天发送一条消息

1. ChatView 调用 useChatSession.sendMessage
2. useChatSession 调用聊天流接口
3. chat_session_service 验证 session、处理附件、写入 user message
4. llm_service 开始流式输出和工具调用
5. 前端按 SSE 事件逐步更新 assistant 消息
6. 聊天结束后 assistant message 落库

### 7.3 技能导入

1. SettingsView 调用 useSkillManager.importSkill
2. api.ts 调用 import-skillhub / import-clawhub / import-zip
3. skill_admin_service 下载或解压技能包
4. 技能写入 data/skill_workspace/skills
5. skill_registry.reload 重新加载
6. 禁用状态从 app_settings.disabled_skill_ids_json 恢复

### 7.4 调度触发

1. scheduler_service 后台线程定期轮询
2. aniu_service.process_due_schedule 识别到期任务
3. 生成对应 run
4. 执行分析或交易
5. 更新 last_run_at、next_run_at、retry_after_at

---

## 8. 常见开发场景怎么改

### 8.1 新增一个后端字段并展示到前端

顺序建议：

1. 改 models.py
2. 改 database.py 兼容补列逻辑
3. 改 schemas/aniu.py
4. 改对应 service 的组装逻辑
5. 改 frontend/src/types.ts
6. 改 api.ts 或 composable 映射
7. 改对应 view
8. 补测试

### 8.2 新增一个设置项

要同时检查这些地方：

- app_settings 表
- AppSettingsRead / AppSettingsUpdate
- aniu_service.get_or_create_settings 和 update_settings
- frontend/src/types.ts 的 AppSettings
- store.settings
- SettingsView 表单
- 如果该设置也支持环境变量默认值，还要改 core/config.py

### 8.3 新增一个页面功能按钮

顺序建议：

1. 先判断已有接口能否复用
2. 如果不能，先加后端接口
3. 再在 api.ts 中补调用函数
4. 把交互逻辑写进 composable
5. 最后在 view 中接线

### 8.4 新增一个任务类型

这类改动影响面大，至少要检查：

- strategy_schedules.run_type
- strategy_runs.run_type
- schemas 中 run_type 的 Literal 枚举
- aniu_service 的 _resolve_run_type / _resolve_manual_run_profile / _infer_run_type
- llm_service 构造工具和系统提示词时是否要区分 run_type
- 前端 types.ts 和分析页展示文本
- 调度页表单和默认任务生成逻辑

### 8.5 修改聊天附件能力

这类改动主要在：

- chat_session_service.py
- chat_attachments 表结构
- 上传接口和下载接口
- 前端 ChatView / useChatSession

要特别注意：

- 文件大小限制
- MIME 白名单
- 文本提取长度限制
- 存储路径兼容

### 8.6 修改技能系统

要重点看：

- skill_admin_service.py
- app_settings.disabled_skill_ids_json
- app/skills 下的 skill_registry
- data/skill_workspace/skills 的目录结构

高风险点：

- 内置技能和工作区技能 id 冲突
- 删除技能时误删管理目录之外的文件
- 引入新元数据但前端兼容信息没同步

---

## 9. 高风险点

### 9.1 数据库兼容迁移靠代码补丁

这是当前项目最重要的维护事实。

只改 models.py 不够，必须同步检查 database.py 的补列和回填逻辑。

### 9.2 service 职责偏重

aniu_service.py 和 chat_session_service.py 都比较重。

如果继续加新功能，建议：

- 优先抽出辅助函数
- 把纯数据转换和纯业务动作分开
- 避免一个函数同时处理路由入参、业务决策和数据组装

### 9.3 前后端类型需要双向同步

后端 schema 改了，如果前端 types.ts、composable 映射没同步，页面会出现静默错误或展示异常。

### 9.4 调度问题通常不是单点问题

调度页看到的只是表单层。真实问题可能分布在：

- cron 表达式生成
- next_run_at 计算
- scheduler_service 轮询
- process_due_schedule
- retry 逻辑

### 9.5 登录和 401 处理已集中在 api.ts

不要在各页面各写一套 401 处理，否则后面会很难维护。

---

## 10. 测试策略

Aniu-main 已经有比一般个人项目更完整的测试骨架。

后端重点测试文件：

- backend/tests/test_chat_api.py
- backend/tests/test_chat_sessions_api.py
- backend/tests/test_service_guards.py
- backend/tests/test_skill_admin_api.py
- backend/tests/test_schedule_timezone.py
- backend/tests/test_runtime_config.py
- backend/tests/test_run_preview_api.py
- backend/tests/test_automation_session.py

前端重点测试文件：

- frontend/tests/api.test.ts
- frontend/tests/useChatSession.test.ts
- frontend/tests/useChatSessions.test.ts
- frontend/tests/useScheduleForm.test.ts
- frontend/tests/useSkillManager.test.ts
- frontend/tests/tasks-view-runs.test.ts
- frontend/tests/login-view.test.ts

开发建议：

- 后端新增 API，优先补后端接口测试
- 前端新增交互逻辑，优先补 composable 测试
- 调度和聊天相关改动，至少跑对应专项测试

---

## 11. 推荐阅读顺序

### 11.1 第一次接手整个项目

1. backend/app/main.py
2. backend/app/api/router.py
3. backend/app/schemas/aniu.py
4. backend/app/db/models.py
5. backend/app/db/database.py
6. backend/app/services/aniu_service.py
7. backend/app/services/chat_session_service.py
8. backend/app/services/skill_admin_service.py
9. frontend/src/services/api.ts
10. frontend/src/router/index.ts
11. frontend/src/stores/legacy.ts
12. frontend/src/composables 下与你需求对应的文件
13. frontend/src/views 下对应页面

### 11.2 按问题类型快速阅读

登录问题：

- core/auth.py
- services/aniu_service.py
- frontend/src/services/api.ts
- frontend/src/views/LoginView.vue

调度问题：

- frontend/src/composables/useScheduleForm.ts
- services/aniu_service.py
- services/scheduler_service.py
- db/models.py

聊天问题：

- frontend/src/composables/useChatSession.ts
- services/chat_session_service.py
- services/llm_service.py
- db/models.py

技能问题：

- frontend/src/composables/useSkillManager.ts
- services/skill_admin_service.py
- app/skills
- data/skill_workspace

运行记录问题：

- frontend/src/composables/useAnalysisRuns.ts
- services/aniu_service.py
- db/models.py 中 strategy_runs / trade_orders

---

## 12. 后续重构建议

如果要持续演进这个项目，最值得做的结构改进有三个：

1. 为数据库演进引入正式迁移机制，减少 database.py 的兼容负担
2. 继续拆分 aniu_service.py 和 chat_session_service.py，降低单文件复杂度
3. 逐步让前端 page logic 更稳定地沉到 composable，保持 view 纯展示

---

## 13. 一句话记忆

改 Aniu-main 时，先判断需求属于哪一类：

- 协议改动：schema + types + api
- 业务改动：service
- 数据改动：models + database 兼容逻辑
- 交互改动：composable + view
- 调度改动：schedule form + aniu_service + scheduler_service
- 技能改动：skill_admin_service + skill_registry + workspace skill dir

按这条线定位，通常能在最短时间内找到真正该改的文件。