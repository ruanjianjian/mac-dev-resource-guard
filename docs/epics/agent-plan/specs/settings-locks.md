# Spec: agent 改设置与锁 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-01 第 3 条、RAW-07。讨论过程：决策卡 https://claude.ai/artifact/NVzY3dsLGW9vDeGQs8Kecg（A02）。

## 意图

用户手动设了某些选项，用得多了，agent 可能发现设得不太对，就自己改（RAW-01 第 3 条）。

- agent 改设置时直接改，记下理由，通知用户一次（RAW-07）。
- 用户能把某一项锁住，锁住的 agent 不碰。
- 用户自己的改动（在看板里，或在对话里亲口要求）不受锁限制，也记成用户的决定。

## 非目标

- 这一版的锁在对话里加（agent 按用户的话运行命令），看板上的锁按钮随 plan-page 一起做。
- agent 不能自己加锁或解锁。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 用户没锁空间目标 | agent 运行 `setting target 60 --why …` | 目标改成 60 GB，记下是 agent 改的、从多少改成多少、为什么，并通知用户一次；没写理由就拒绝 | CLI后端 | ✅ | auto: test_agent_plan.CommandTest.test_the_agent_changes_an_unlocked_setting_and_says_so（60 GB、通知里写「空间目标：… → 60 GB」、没理由拒绝） |
| AC-02 | [核] | 用户说「目标我自己定」 | agent 运行 `setting target lock --words "原话"` | 目标被锁住，记一条用户的决定（带原话） | CLI后端 | ✅ | auto: RulesTest.test_a_lock_stops_the_agent_not_the_user、CommandTest.test_a_locked_setting_is_refused_to_the_agent_with_a_reason_to_pass_on |
| AC-03 | [核] | 目标或某条规则被锁住 | agent 想改 | 拒绝，提示 agent 把理由告诉用户、请用户决定要不要解锁 | CLI后端 | ✅ | auto: 同 AC-02；auto: LockAndSettleFindingsTest.test_5_only_the_users_words_lock_unlock_or_speak_for_the_user、test_6_a_switch_that_moves_a_locked_one_is_refused |
| AC-04 | [核] | 目标被锁住 | 用户亲口要求改（`--by user --words "原话"`），或在看板里改 | 照改；记成用户的决定；不给用户发「计划改了」的通知（是用户自己改的） | CLI后端、mac原生 | ✅ | auto: CommandTest.test_a_locked_setting_is_refused_to_the_agent_with_a_reason_to_pass_on（改成 30 GB）、CommandTest.test_a_change_in_the_panel_is_the_users、RulesTest.test_a_lock_stops_the_agent_not_the_user（锁定后用户改规则照改、无通知） |
| AC-05 | [边] | 开关类设置（共享构建缓存、自动清理、外接盘、自动更新、等你确认） | agent 用 `setting <名字> on` 或 `off`，带 `--why …` | 和用户在看板里点开关的效果一样，记下谁改的、为什么 | CLI后端 | ✅ | 实现：service.change_setting 复用看板的 checkup.apply / turn_off；auto: 目标一项见 AC-01 |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-30 | 新建（RAW-01、RAW-07） | [需求变更] |
| CH-02 | 2026-10-30 | Codex 独立评审 CR-01（2026-10-30）查出，均先写测试复现再修：锁、解锁和「用户亲口要求」都必须带用户原话，agent 自己不能解锁；旧的 `target 30` 命令也走同一道检查（锁、定稿、理由）；打开或关闭「自动清理」会连带「等你确认」，两项都查锁、都记录（AC-02～05） | [缺陷] |
