---
name: memory
description: 长期交易记忆的读写工具：形成判断前检索历史经验，值得复用的教训写入记忆。
metadata:
  aniu:
    handler_module: skills.memory.handler
    category: memory
---

# 交易记忆技能（memory）

形成买入、卖出、调仓、撤单或观望判断前，应主动调用 `memory_read` 查询相关经验（如标的代码、行业、主题、历史教训关键词），参考历史经验修正本次判断。

如果本次运行产生了值得长期复用的经验或教训（例如某次判断成功或失败的原因、某类标的的行为规律、某类消息面的实际影响），在给出最终结论前调用 `memory_write` 保存简洁的投资经验教训和形成原因。不得保存运行总结、短暂行情或泛泛原则。

更新或删除已有记忆时必须带上最近读取到的 `expected_version`，版本冲突时先重新读取再操作。

## 工具

- `memory_read`：按关键词检索长期交易记忆，返回记忆内容与版本号。
- `memory_write`：create 新建 / update 更新 / delete 软删除记忆，带乐观锁版本控制。
