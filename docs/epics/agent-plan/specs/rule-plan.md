# Spec: 计划变成规则清单 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-01 第 2、3 条、RAW-02、RAW-05。讨论过程：决策卡 https://claude.ai/artifact/NVzY3dsLGW9vDeGQs8Kecg（M03、F01）。

## 意图

清理计划要能跟着记忆和用户偏好改（RAW-01 第 2 条），所有清理配置都变成 agent 能用的能力（RAW-01 第 3 条）。

**计划是一份规则清单**（RAW-05）。每类东西一条规则，写明做法、谁定的、为什么、依据哪几条记忆。做法有五种：

- 挪到外接盘
- 自动清
- 先问
- 只列出、不催
- 不管

后台照清单执行，agent 用命令改。另有一份「先别动」名单，名单上的路径，后台挪、清、列「等你确认」时都跳过。

**安全底线写在代码里，清单改不动：**

- 任务、模拟器、安装包这些会丢东西的，最多设成「先问」；
- 正在用的不动；
- 有没推送改动的不挪。

**第一批规则就是现在的做法。** 其中模拟器多一条：只被「看画面」或「看日志」的程序挂着、超过 12 小时的，算没人用，列进「等你确认」（RAW-02：10-30 五台模拟器这样挂了快一天）。

## 非目标

- 不让任何规则自动删任务、模拟器或用户的文件（RAW-05）。
- 这一版不改界面；「帮你管着」改成计划页在 plan-page 里做。
- 不改内存守护自己关模拟器的规则（它只在内存紧张、没人连着满两小时时关）。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 刚升级，还没有计划文件，或计划文件坏了 | 后台按计划执行 | 每类东西用现在的做法，行为和升级前一样 | CLI后端 | ✅ | auto: test_agent_plan.RulesTest.test_defaults_are_todays_behaviour；全量回归 434 项通过；auto: ReviewFindingsTest.test_10_a_readable_but_wrong_plan_file_degrades_entry_by_entry |
| AC-02 | [核] | agent 发现用户每次都拒绝删旧模拟器 | 运行 `rule simulator-delete quiet --why … --evidence …` | 规则改成「只列出、不催」，记下是 agent 改的、为什么、依据哪几条记忆；给用户发一条「资源看板的计划改了」通知，写明从什么改成什么和理由；没写理由就拒绝 | CLI后端、mac原生 | ✅ | auto: RulesTest.test_agent_changes_need_a_reason_are_logged_and_said_once；通知由菜单栏 App 读 notice-request.json 发出（App 自检 PASS）；auto: ReviewFindingsTest.test_12_two_changes_are_both_said_and_a_change_of_hours_is_visible |
| AC-03 | [核] | 有人想把一类会丢东西的东西设成自动删或自动清，或者把任务设成「自动清」 | 改规则 | 拒绝，说明这一类只能设成哪几种（安全底线） | CLI后端 | ✅ | auto: RulesTest.test_the_safety_floor_cannot_be_crossed、CommandTest.test_a_locked_setting_is_refused_to_the_agent_with_a_reason_to_pass_on（命令行同样拒绝） |
| AC-04 | [核] | 某一类设成「只列出、不催」或「不管」 | 后台刷新「等你确认」 | 「只列出、不催」的照样列着、但不为它发通知；「不管」的不列 | CLI后端 | ✅ | auto: ExecutorTest.test_quiet_kinds_are_listed_without_a_nudge_and_off_kinds_not_at_all；auto: ReviewFindingsTest.test_11_quiet_items_never_nudge_through_the_agent_notice、test_2_a_stale_confirmation_cannot_run_what_the_plan_now_leaves_alone |
| AC-05 | [核] | 「先别动」名单上有一个任务 | 后台挪闲置任务、挪构建产物、列「等你确认」、算计划 | 都跳过它，挪任务的记录里写「先别动：理由」；用户加的只有用户能拿掉 | CLI后端 | ✅ | auto: ExecutorTest.test_kept_paths_are_left_alone_by_every_part、test_the_plan_page_follows_the_rules；auto: ReviewFindingsTest.test_1、test_3、test_9、test_13，ToolCacheKeepTest.test_8_a_kept_tool_cache_is_not_moved |
| AC-06 | [核] | 「闲置任务」「工具缓存」「任务构建产物」「Xcode 中间缓存」某一条设成「不管」 | 后台下一轮 | 对应那一类不再挪、不再清 | CLI后端 | ✅ | auto: ExecutorTest.test_idle_tasks_set_to_off_stay_on_the_internal_disk；工具缓存、构建产物、Xcode 中间缓存三处同一个判断（service.tick_caches、clean_caches） |
| AC-07 | [核] | 一台开着的模拟器，只被一个「看画面」或「看日志」的程序挂着 | 那个程序已经跑了 12 小时以上，再过 4 小时 | 列进「等你确认」：要不要关这台模拟器；挂着不到 12 小时的，或有构建、测试程序连着的，照旧算在用 | CLI后端 | ✅ | auto: ExecutorTest.test_a_simulator_held_only_by_a_long_running_stream_is_asked_about（baguette stream 13 小时、log stream 30 小时列出；1 小时、xcodebuild 不列）；本机实测 2026-10-30 19:43（main c6f54cf）：RollingGo r3 Flutter 只被跑了 1 天 2 小时的 baguette stream 挂着，开始算没人用；r3 Native（stream 1 小时 53 分）、TBG Gray（12～17 分钟前刚开的看画面和看日志）照旧算在用；auto: ReviewFindingsTest.test_14_a_path_holding_the_words_is_not_a_stream |
| AC-08 | [核] | 有计划 | agent 运行 `resource-guard-service rules` | 列出每条规则的做法、可选做法、谁定的、为什么、锁没锁，以及「先别动」名单 | CLI后端 | ✅ | auto: CommandTest.test_the_agent_reads_and_changes_the_plan_with_commands |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-30 | 新建（RAW-01、RAW-02、RAW-05） | [需求变更] |
| CH-02 | 2026-10-30 | Codex 独立评审 CR-01（2026-10-30）查出，均先写测试复现再修：「先别动」各处统一判断（路径本身、它所在的目录、它里面有被保护的东西都算；相对路径存成绝对路径；模拟器按它的数据目录）；执行「等你确认」前再按规则和「先别动」核对一遍，过时的确认不再生效；清 Xcode 中间缓存、搬工具缓存也看「先别动」；agent 不能用同一路径覆盖用户的「先别动」；「看画面」判断只认程序本身，不被路径里的词骗；计划页只算真会挪的那一份；「只列出、不催」的项不再通过 agent 通知催用户；连续两次改动的通知都发出，改小时数也看得见；计划文件里类型不对的值逐项丢弃、不崩（AC-02～07） | [缺陷] |
