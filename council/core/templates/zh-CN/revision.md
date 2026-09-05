你现在处于 **P6 修订** 阶段，你是主案作者 `{{node_id}}`。

请将 V{{version}} 修订为 V{{next_version}}。

## 硬性约束

1. 必须输出**完整**的 V{{next_version}} 正文（`proposal` 字段），**禁止只给修改日志**。
2. 仍然只能有一套方案：禁止子方案、备选、分支、"或者也可以"。
3. 必须解决所有 `must` 级别问题；认为评审判断有误的，可以拒绝，但必须在 `open_questions` 或 `assumptions` 中写明理由。
4. 不得扩大原任务范围，不得改变原始问题目标，不得虚构已经发生的现实验证。
5. `change_summary` 用几句话说明 V{{version}} → V{{next_version}} 的关键变化。

## 数据区

{{data_zones}}
