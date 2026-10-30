# Spec: 资源管理记忆 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-01 第 1 条、RAW-03、RAW-04。讨论过程：决策卡 https://claude.ai/artifact/NVzY3dsLGW9vDeGQs8Kecg（M01、M02）。

## 意图

用户装好以后，会不断地清缓存、关模拟器、决定什么留什么删（RAW-01 第 1 条）。这些决定要记下来，成为「资源管理 agent」的记忆，agent 以后改计划时有据可依。

- 记忆放在资源看板这里，Claude、Codex 读写同一份（RAW-03）。
- 只记两样（RAW-04）：
  - 用户的决定，有原话就带原话；
  - agent 学到的教训。
- 后台自动做的动作照旧只进日志，不进记忆。

## 非目标

- 不放进各个 agent 自己的记忆里（RAW-03）。
- 不上传，不跨机器同步。
- 不记后台每一次自动挪、自动清的动作（RAW-04）。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 用户在「等你确认」里 | 确认处理或忽略一项 | 自动记一条用户的决定：哪一类东西、哪一项、确认还是忽略、当时的理由；记忆写不进去也不影响这次处理 | CLI后端 | ✅ | auto: test_agent_plan.MemoryTest.test_yes_and_no_in_the_confirm_list_are_remembered |
| AC-02 | [核] | 用户在「帮你管着」里 | 改空间目标或某个开关 | 自动记一条用户的决定，写明从什么改成什么 | CLI后端、mac原生 | ✅ | auto: test_agent_plan.CommandTest.test_a_change_in_the_panel_is_the_users（走看板后端改目标 60 GB） |
| AC-03 | [核] | 用户在对话里说了一个取舍 | agent 运行 `resource-guard-service memory add --kind decision --words "原话"` | 记一条用户的决定，原话原样保存；agent 自己学到的用 `--kind lesson` 记教训 | CLI后端 | ✅ | auto: test_agent_plan.CommandTest.test_the_agent_reads_and_changes_the_plan_with_commands、MemoryTest.test_decisions_and_lessons_only_with_words_or_a_note |
| AC-04 | [边] | agent 想记一条 | 种类不是「决定」或「教训」，或没写关于什么，或原话和说明都没有 | 拒绝，说明缺什么 | CLI后端 | ✅ | auto: MemoryTest.test_decisions_and_lessons_only_with_words_or_a_note |
| AC-05 | [核] | 有记忆 | agent 运行 `resource-guard-service memory`（可按关于什么、最近几条筛） | 按时间顺序列出，每条有编号，改计划时可以引用编号作依据 | CLI后端 | ✅ | auto: 同 AC-03 |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-30 | 新建（RAW-01、RAW-03、RAW-04） | [需求变更] |
