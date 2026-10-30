# Spec: 「帮你管着」改成计划页 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-01 第 3 条、RAW-10、RAW-15、RAW-16。讨论过程：决策卡 https://claude.ai/artifact/NVzY3dsLGW9vDeGQs8Kecg（S02）；视觉稿三版（RAW-15），用户选 B「只看改过的」（RAW-16）。

## 意图

面向人的那一层是次要的（RAW-01 第 3 条），「帮你管着」从一排开关变成 agent 的计划，主要回答两件事：计划现在是什么状态，谁改了什么、为什么（RAW-10）。

按视觉稿 B，页面从上到下是：

1. 一张深蓝小卡：计划第几版、还在调整还是已定稿、记了几条你的决定、下次哪天复查。
2. 只列「改过的」：
   - 空间目标（总在第一行）；
   - 你或 agent 改过、或你锁住的规则和设置；
   - 「先别动」的东西。

   每行写谁定的、为什么，右边是做法标签和锁。
3. 其余照默认的收成一行「其余 N 类照默认 ›」，点开才看。
4. 底部：「高级 ›」进原来的开关，「交给 agent ›」。

## 非目标

- 不在计划页上直接改做法：做法由 agent 按记忆改，或你在对话里说；页面上只锁和解锁。
- 原来的开关不删，收进「高级」。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 计划还在调整 | 点封面「帮你管着 ›」 | 顶部深蓝卡写「计划第 N 版 · 还在调整」和「记了 N 条你的决定 · 几号复查」；N 版 = 1 + 改过计划的复查次数 | CLI后端、mac原生 | ✅ | auto: test_agent_plan.PlanPageTest.test_the_state_on_top_what_changed_below_and_the_rest_folded；ui: one-click-evidence/plan-page/1-plan.png（离屏渲染真实视图） |
| AC-02 | [核] | 你锁了目标，agent 改过一条规则，你说过一个任务先别动 | 看计划页 | 只列这几行：空间目标在第一行，写「你锁住了」；agent 改的那条写「agent 改的 · 理由」并带做法标签；「先别动」的任务去掉日期前缀显示名字，写「你说先别动 · 理由」 | CLI后端、mac原生 | ✅ | auto: 同 AC-01；ui: 1-plan.png |
| AC-03 | [核] | 有规则没人改过 | 看计划页 | 收成一行「其余 N 类照默认」，点开逐行列出，每行带做法标签和锁 | mac原生 | ✅ | ui: one-click-evidence/plan-page/4-fresh-defaults.png（展开后） |
| AC-04 | [核] | 想手动开关某一项 | 点「高级 ›」 | 进原来的五行设置页（目标、共享构建缓存、自动清理、外接盘、自动更新），标题「高级」，返回到「帮你管着」 | mac原生 | ✅ | ui: one-click-evidence/plan-page/3-advanced.png |
| AC-05 | [核] | 在计划页上 | 点某条规则或设置旁的锁 | 锁住（或解锁），记一条你的决定（来源是看板）；锁住后 agent 改不了它；「先别动」的行不能锁（本来就只有你能拿掉） | CLI后端、mac原生 | ✅ | auto: PlanPageTest.test_a_lock_clicked_in_the_panel_is_the_users_decision |
| AC-06 | [核] | 计划已定稿 | 看计划页 | 顶部卡写「计划已定稿」和「定稿于几号 · 出现新东西时才再改」 | CLI后端、mac原生 | ✅ | auto: PlanPageTest.test_a_settled_plan_says_so |
| AC-07 | [核] | 在计划页上 | 点「交给 agent ›」 | 和计划页原来的「交给 agent」一样：agent App 打开新对话，要说的话已经填好，按回车发送（agent-handoff AC-04） | mac原生 | ✅ | 实现：沿用 CoverStore.handOff（agent-handoff AC-04） |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-30 | 新建（RAW-01、RAW-10、RAW-15、RAW-16），按视觉稿 B 实现 | [需求变更] |
| CH-02 | 2026-10-30 | 「交给 agent ›」改为打开新对话、话已填好，不再复制粘贴（agent-handoff CH-04） | [需求变更] |
