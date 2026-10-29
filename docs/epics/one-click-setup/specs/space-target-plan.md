# Spec: 空间目标与计划 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-22～25。讨论过程：决策卡 https://claude.ai/artifact/KYuFV6eBFYkwm4XCrb3aJf（三轮 16 张，用户 2026-10-28 看完）。

## 意图

用户想自己定「内置盘至少留多少」，比如 50 GB（RAW-22）。工具按这个目标、结合本机实际情况出一份计划：哪些它会自动做、哪些要你点头、哪些需要判断，各能腾出多少，全做完能到多少（决策卡 P01～P03）。目标只有一个数：低于它就按计划处理（P01）。全做完也不够时，明说还差多少并给建议，不放宽规则（P03）。

## 非目标

- 不为了凑够目标去放宽三档规则（P03）。
- 不承诺一定达到目标：计划只算规则允许做的事。
- 计划只算内置盘：挪到外接盘算腾出。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 打开「帮你管着」 | 改「空间目标」 | 可以设成任意 GB；第一次体检时按磁盘大小给一个建议值（约 20%，取整到 10 GB，至少 20 GB）；改完封面和后台立刻按新目标算 | mac原生、CLI后端 | ✅ | ui: one-click-evidence/cover/6-switches.png（空间目标 50 GB，可加减）；auto: test_target_plan 的 test_target_is_validated_and_written_as_one_set、test_one_number_drives_the_three_lines、BackendPlanTest（改成 60 GB 后计划页随之变化） |
| AC-02 | [核] | 设了目标 50 GB | 剩余空间低于 50 GB | 自动档开始按计划处理，处理到比目标多 10% 就停；封面变暖色；低于目标的一半时变红并马上提醒；过期 Xcode 缓存清理也按这组线 | CLI后端、mac原生 | ✅ | auto: test_one_number_drives_the_three_lines（50 → 提醒线 50、停止线 55、危险线 25），自动档和菜单栏颜色读的就是这组线（test_tiers、App 自检 free-space colour levels） |
| AC-03 | [核] | 设了目标 | 打开封面 | 封面多一行「目标 50 · 还差 31 GB」，已达到时写「目标 50 · 已达到」；点这一行进计划页 | mac原生 | ✅ | ui: one-click-evidence/cover/2-tight.png（「目标 50 GB · 还差 31 GB ›」） |
| AC-04 | [核] | 低于目标 | 打开计划页 | 分三段写，每段写能腾出多少、列出主要的几项：会自动做的、等你点头的、需要判断的（同一任务里一周以上没用过的 Xcode 构建副本，一周内用过的和最新的一份不列；容器镜像等）；最后一句写「全做完能到 N GB」 | mac原生、CLI后端 | ✅ | ui: one-click-evidence/cover/7-plan.png；auto: test_three_sections_and_how_far_they_reach；实测本机：等你点头 7.3 GB（没在用的 iOS 27.1 运行时）、需要判断 45 GB（3 个任务里多余的 Xcode 构建副本），全做完能到 94 GB；auto: test_copies_used_within_a_week_are_never_offered、test_xcode_record_of_last_use_wins_over_folder_times；实测本机 9 份副本都在一周内用过，需要判断为空 |
| AC-05 | [边] | 三段全做完也到不了目标 | 打开计划页 | 写明还差多少，列出最大的几项和具体建议（收尾不用的任务、换更大的外接盘等），不改闲置天数、不扩大自动档 | mac原生、CLI后端 | ✅ | auto: test_shortfall_is_said_with_suggestions_not_wider_rules |
| AC-06 | [核] | 有计划 | 本地 agent 读计划 | 有一条只读命令输出同一份计划（JSON），内容和计划页一致 | CLI后端 | ✅ | auto: BackendPlanTest；命令 resource-guard-service plan --json 与计划页同源（service.current_plan） |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-28 | 新建（RAW-22～25） | [需求变更] |
| CH-02 | 2026-10-29 | 规则：一周内用过的 Xcode 构建副本不列入「需要判断」，每个任务最新的一份也不列；「上次用」取 Xcode 自己记的时间、最近构建日志和目录时间中最晚的（RAW-32～34） | [需求变更] |
| CH-03 | 2026-10-29 | 建议目标的写法跟上 1.7.4 的实际做法：约磁盘的 20%（取整到 10 GB，至少 20 GB），新装默认就用它（RAW-41；见 one-line-install CH-03）（AC-01） | [文档对齐] |
