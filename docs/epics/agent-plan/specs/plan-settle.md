# Spec: 计划定稿 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-01 第 5 条、RAW-09。讨论过程：决策卡 https://claude.ai/artifact/NVzY3dsLGW9vDeGQs8Kecg（S01）。

## 意图

到了某个临界点，计划就不该再变；清理计划不需要一直迭代，肯定有界限（RAW-01 第 5 条）。

**定稿的标准**（RAW-09）：连续 4 次复查都没改计划，并且这期间用户没有手动纠正过。手动纠正指：在看板里改开关或目标、要求改规则、加「先别动」。在「等你确认」里确认或忽略，是在回答计划的提问，不算纠正。

**定稿时：**

- 资源看板通知用户；
- agent 改计划会被拒绝；
- 每周复查只看有没有出现新东西。

**只在这三种情况下重新打开：**

- 出现新类型的占用；
- 换了硬盘或外接盘；
- 用户要求。

agent 要先说明理由再打开；用户自己改了计划，计划自动重新打开。

## 非目标

- 不按「装好满 30 天」之类的固定时间冻结（决策卡 S01 没选 B）。
- 定稿不停掉后台的整理：规则照旧执行，只是不再改。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 最近 4 次复查都没改计划，这期间用户没手动纠正过 | 第 4 次复查记完 | 计划定稿，发通知「资源看板的计划定稿了」，写明以后什么时候才会改 | CLI后端、mac原生 | ✅ | auto: test_agent_plan.ReviewTest.test_the_plan_settles_after_four_quiet_reviews_without_corrections |
| AC-02 | [核] | 这期间用户在看板里改过目标 | 第 4 次没改的复查记完 | 不定稿；从那次纠正之后重新数 4 次 | CLI后端 | ✅ | auto: 同 AC-01（第 5 周纠正，第 8 周才定稿） |
| AC-03 | [核] | 计划已定稿 | agent 想改规则、改设置或加「先别动」 | 拒绝，说明只有新类型的占用、换盘或用户要求才改，并告诉它先用 `review reopen --why` 说明原因 | CLI后端 | ✅ | auto: ReviewTest.test_a_settled_plan_refuses_the_agent_until_reopened_with_a_reason |
| AC-04 | [核] | 计划已定稿 | agent 运行 `review reopen --why "新出现了…"` | 计划重新打开，记下原因，通知用户；没写原因就拒绝 | CLI后端 | ✅ | auto: 同 AC-03 |
| AC-05 | [核] | 计划已定稿 | 用户自己改了一条规则或一个设置 | 照改，计划自动重新打开，记下「用户自己改了…」 | CLI后端 | ✅ | auto: ReviewTest.test_the_user_changing_a_settled_plan_opens_it_again |
| AC-06 | [边] | 计划已定稿 | agent 每周复查 | 复查材料写明已定稿、只看有没有新类型的占用或换盘，没有就记「没改」 | CLI后端 | ✅ | auto: ReviewTest.test_the_plan_settles_after_four_quiet_reviews_without_corrections（定稿后材料里的步骤） |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-30 | 新建（RAW-01、RAW-09）：「决定都和计划一致」落成「这期间没有手动纠正」，确认或忽略不算纠正 | [需求变更] |
