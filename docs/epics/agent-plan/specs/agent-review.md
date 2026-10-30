# Spec: agent 什么时候来 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-01 第 2、4 条、RAW-06。讨论过程：决策卡 https://claude.ai/artifact/NVzY3dsLGW9vDeGQs8Kecg（F02、A01）。

## 意图

agent 平时不在线，要有人把它叫来，它才能按记忆更新计划（RAW-01 第 2 条）。两种时候叫它（RAW-06）：

- **出事时**：跌破危险线、自动能做的都做完了仍差目标、某处一天涨 10 GB，资源看板发通知，用户点一下交给 agent（沿用已有做法）。
- **每周复查**：
  - agent 有定时任务能力的（Claude 桌面的定时任务、Codex 的自动化），装好时自己建一个每周的复查，并告诉资源看板「建好了」；
  - 没建，或建了但连续 9 天没来，资源看板每周发一条「该复查了」。

复查时，agent 读一份「复查材料」：上次复查以来用户的决定、它记下的教训、计划改过什么。按步骤改计划，最后记下这次复查的结论，并告诉用户。

## 非目标

- 资源看板不替 agent 建定时任务：两边的定时任务只能由 agent 用自己的工具建。
- 后台不无人值守地跑 agent（决策卡 A01 没选 C）。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 上次复查（或第一版计划开跑）已满 7 天，agent 没登记定时复查 | 后台下一轮 | 发一条「资源看板每周复查」（第一次是「资源看板第一周复查」），写明这段时间记了几条用户的决定；点一下交给 agent；一周最多发一次 | CLI后端、mac原生 | ✅ | auto: test_agent_plan.ReviewTest.test_the_first_review_is_owed_a_week_after_the_first_plan_and_nudged_once |
| AC-02 | [核] | agent 用自己的定时任务建好了每周复查，运行 `review scheduled claude`（或 codex） | 每周 | 资源看板不再每周发通知；只有连续 9 天没复查时才发 | CLI后端 | ✅ | auto: ReviewTest.test_a_review_in_the_agents_own_scheduler_gets_two_days_grace、ReviewCommandTest.test_the_review_commands |
| AC-03 | [核] | 到了复查的时候 | agent 运行 `review` | 拿到复查材料：上次复查以来的决定和教训、计划的变化、现在的规则，以及要做的步骤 | CLI后端 | ✅ | auto: ReviewTest.test_the_review_packet_holds_what_happened_since_the_last_review |
| AC-04 | [核] | agent 复查完 | 运行 `review done --changed yes/no --summary "…"` | 记下这次复查改没改计划和结论；没写结论或没说改没改就拒绝 | CLI后端 | ✅ | auto: ReviewCommandTest.test_the_review_commands、ReviewTest.test_the_review_packet_holds_what_happened_since_the_last_review |
| AC-05 | [核] | 用户点了任何一条「交给 agent」的通知 | agent 收到复制好的那句话 | 那句话先让 agent 运行 `review` 复查，再读计划处理「等你点头」和「需要判断」 | CLI后端、mac原生 | ✅ | auto: ReviewCommandTest.test_the_review_commands（交接那句话里有 review）、BackendPlanTest.test_target_plan_rows_and_handoff_words |
| AC-06 | [边] | 定时任务每周一触发，但第一版计划还没满一周（或离上次复查不到一周） | agent 运行 `review` | 复查材料写明「还没到复查时间（下次是几号）」：定时任务来的只回一句就结束，不记复查；用户主动要求时照常复查 | CLI后端 | ✅ | auto: test_agent_plan.ReviewTest.test_a_scheduled_run_before_the_first_week_is_told_to_wait |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-30 | 新建（RAW-01、RAW-06）；查明 Claude 桌面定时任务和 Codex 自动化只能由 agent 自己的工具建，改为 agent 自建 + 资源看板每周提醒兜底 | [需求变更] |
| CH-02 | 2026-10-30 | 在本机 Claude 建每周一的复查时发现：第一次复查要等满一周，周一的定时任务可能来早；复查材料加「到没到时间」（AC-06） | [实现引入] |
