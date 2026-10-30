# Spec: 首次安装就出计划 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-01 第 4 条、RAW-08。讨论过程：决策卡 https://claude.ai/artifact/NVzY3dsLGW9vDeGQs8Kecg（A03）。

## 意图

首次安装之后就该是这样：装好、连上本地的 agent，agent 出计划、改配置、开跑，有了用户的记忆后再更新计划（RAW-01 第 4 条）。

- 新用户不知道适合自己的计划是什么样，所以先跑再学：第一版计划就是现在这套安全规则，agent 只问一句就开跑，不追问偏好（RAW-08）。
- 第一周把用户的每个取舍都记下来。
- 第一周结束的那次复查出修订版，讲清这一周学到了什么、改了什么、为什么。

## 非目标

- 不在安装时追问一串偏好问题（RAW-08，决策卡 A03 没选 B）。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 新装或升级到这一版 | 后台第一次整理计划 | 记下第一版计划的开跑时间，第一次复查定在一周后；第一版计划就是现在的安全规则 | CLI后端 | ✅ | auto: test_agent_plan.ReviewTest.test_the_first_review_is_owed_a_week_after_the_first_plan_and_nudged_once、RulesTest.test_defaults_are_todays_behaviour；实现：service.refresh_plan 调 agent_plan.ensure_started |
| AC-02 | [核] | agent 按 SKILL.md 装好 | 装完 | 只问一句（缓存 + 目标）就开跑，告诉用户「一周后复查一次，按这一周的选择出修订版」；有定时任务能力的，建好每周复查并登记 | CLI后端 | ✅ | SKILL.md 第 1 节第 7、8 步；登记命令见 agent-review AC-02 |
| AC-03 | [核] | 第一周结束 | agent 运行 `review` | 复查材料写明这是第一周结束的复查，要出修订版并讲清学到了什么、改了什么 | CLI后端 | ✅ | auto: ReviewTest.test_the_review_packet_holds_what_happened_since_the_last_review（first_review、「第一周结束的复查」） |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-30 | 新建（RAW-01、RAW-08） | [需求变更] |
