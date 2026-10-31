# Spec: 告急时 agent 自己来 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-18～24（10-31 内置盘跌到 4 GB、随后内存吃紧那次）；入口和「别人装上就能用」依据 RAW-26～28。

## 意图

资源管理 agent 的目标是**这台 Mac 不卡**：内置盘够用，内存不吃紧（RAW-21）。计划里的规则是起点，不是边界。

10-31 下午内置盘跌到 4 GB：后台每 30 分钟跑一轮，一整天只清出约 1 MB（RAW-18）。真正占空间的是 101 GB 模拟器、PR 已合入的任务目录，还有 10-29 留下的 Codex 后台，它们一直开着那些任务目录，让任务看起来像在用。规则一样都没碰到，agent 也只在凌晨 04:46 被叫过一次。用户问「为啥我的这个资源清理助手根本没有在自己干这些活儿呢」（RAW-20），并否决了「再加几条规则」的改法（RAW-21）。

所以：

- **告急时 agent 自己启动**（RAW-22），不等用户点通知。告急有两种：
  - 内置盘跌破危险线；同一次下跌里，每再跌一档（10 GB、5 GB）再来一次；
  - 内存吃紧：系统内存压力到「严重」，或「警告」连续 30 分钟。
- **agent 来了自己想办法**：它拿到一份告急材料：硬盘和内存的现状、最大的几样东西和证据、用户以前的决定。然后它判断怎么处理：
  - 不丢东西的，直接交给后台去做：关掉没人管的残留后台、把闲置任务挪到外接盘；
  - 会丢东西的，列成一张紧急清理单放进「等你确认」，用户点一次头。
- **越用越懂用户**：用户在紧急清理单上点的确认和忽略，都记成用户的决定。下次告急时，agent 照这些偏好出单（RAW-20：「开着但 3 小时没人连的，不要关机。其他的都可以。」）。
- **一点就到 agent 的对话**（RAW-26、RAW-27）：封面、通知都能直接打开处理这次告急的那段对话。
- **别人装上就能用**（RAW-28）：不依赖这台 Mac 的特殊设置。只用 Claude 的人还差一步「登录一次 Claude Code」，资源看板在第一次告急时提醒、点一下就去登录；新装时 agent 也会顺带办好。

## 非目标

- **安全底线不变**：
  - 会丢东西的删除还是要用户点头；
  - 有未提交或未推送改动的不删；
  - 正在用的不动；
  - 判断不了的保留。
- **平时不自己启动。** 只在告急时自己启动；每周复查照旧走 [agent 什么时候来](agent-review.md)。
- **不内置模型，不替用户登录。** 用的是用户自己的 agent 和它自己的登录。Claude 桌面 App 的登录不给后台用，Claude 这边要用户在终端登录过一次 Claude Code；没登录就换 Codex，两个都跑不了，退回成「交给 agent」的通知。
- **不做「一键释放内存」。** 不 purge、不删 swap、不强杀用户的 App。只关「父进程已经退出、放了一天以上、这段时间没动静」的残留后台。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 内置盘刚跌破危险线 | 后台下一轮 | 自己启动一次 agent；同一次下跌里，再跌破 10 GB、5 GB 时各再启动一次；回到危险线以上后重新计 | CLI后端 | ✅ | auto: test_emergency.TriggerTest.test_each_deeper_drop_starts_the_agent_once |
| AC-02 | [核] | 内存压力到「严重」，或「警告」连续 30 分钟 | 后台下一轮 | 自己启动一次 agent；之后 6 小时内不因为内存再启动 | CLI后端 | ✅ | auto: TriggerTest.test_memory_pressure_starts_the_agent_after_half_an_hour_of_warning |
| AC-03 | [边] | 上一次 agent 还没结束，或 24 小时内已经启动过 4 次 | 又到告急 | 不再启动 | CLI后端 | ✅ | auto: TriggerTest.test_no_second_run_while_one_is_going_and_four_a_day_at_most |
| AC-04 | [核] | 告急 | 启动 agent | 用 `agent-app` 记下的那个；没记就先 Claude、后 Codex。只用能在后台跑的：Claude 要 Claude Code 登录过，Codex 要已登录，Codex 的这次对话在 Codex 里看得到。都跑不了就退回成「交给 agent」的通知 | CLI后端 | ✅ | auto: RunnerTest.test_the_recorded_agent_first_then_the_other_then_the_notice、RunnerTest.test_claude_runs_with_only_the_emergency_commands、RunnerTest.test_codex_starts_a_visible_thread_in_the_run_folder；实测（10-31 17:39，本机）：Codex 跑完一轮，Codex 的会话索引里这条对话名叫「资源看板告急处理」、来源标记为 user，与桌面里看得到的 Companion 对话相同（桌面侧栏在后台未刷新，没把 Codex 拉到前台）；Claude 这台没登录过 Claude Code，按规则改用 Codex |
| AC-05 | [核] | agent 被叫来 | 运行 `rgs emergency show` | 拿到告急材料：<br>• 为什么叫它，硬盘和内存的现状；<br>• 最大的几样东西和证据：模拟器（多大、多久没开、开着没、哪个任务的）、任务目录（多大、改动推没推、PR 合没合、谁开着它）、没人管的残留后台（放了多久、占多少内存、开在哪）；<br>• 用户以前的决定和教训、现在的规则；<br>• 要做的步骤 | CLI后端 | ✅ | auto: MaterialTest.test_the_material_holds_the_evidence_and_the_users_choices；实测：材料几秒内存下，列出 9 个残留后台、9 个内置盘上的任务、21 台模拟器 |
| AC-06 | [核] | agent 判断某个残留后台没人管，或某个任务该挪到外接盘 | `rgs emergency stop <pid> --why` 或 `rgs emergency move <路径> --why` | agent 结束后，后台先复核再做：<br>• 残留后台：还是同一个进程、父进程已退出、不是 launchd 管着的服务、放了 24 小时以上、这段时间几乎没用 CPU，才连同子进程一起关掉；<br>• 任务：没人在用、改动都推了、外接盘插着且够大，才挪过去，不受「闲置满 3 天」限制；<br>不满足就跳过，并写明原因 | CLI后端 | ✅ | auto: ApplyTest.test_an_orphan_is_stopped_only_if_it_is_still_the_same_idle_orphan、ApplyTest.test_a_task_moves_without_the_idle_days_but_never_with_unsaved_work；实测（10-31 17:40）：agent 列了 3 个 10-29 的 Codex 后台，后台复核后关掉，`ps` 核实已不在 |
| AC-07 | [核] | agent 判断某个模拟器或任务该删 | `rgs emergency ask <id> --why` | agent 结束后放进「等你确认」，理由写「agent：…」。再发一条通知「agent 处理了告急」，写明做了什么、还有几项等你确认、约多少 GB；点开就是「等你确认」 | CLI后端、mac原生 | ✅ | auto: ApplyTest.test_asks_land_in_the_pending_list_with_one_notice、ApplyTest.test_agent_asks_survive_the_next_refresh |
| AC-08 | [核] | 用户确认了 agent 列的一项 | 执行前复核 | 只按安全底线复核：没人在用、没被保护、改动都推了，模拟器已关机。不套「关机满 7 天」「闲置满 7 天」。任务里不在代码仓库里的文件（笔记、截图、日志）挪到外接盘的「收尾的任务」里；外接盘没插就跳过，不删 | CLI后端 | ✅ | auto: ConfirmTest.test_a_confirmed_agent_ask_is_rechecked_against_the_floor_only、ConfirmTest.test_notes_beside_the_code_go_to_the_drive_not_the_bin |
| AC-09 | [核] | 用户以前确认或忽略过 agent 列的项，或在对话里说过偏好 | 下次告急 | 这些决定都在告急材料里；agent 照它们出单 | CLI后端 | ✅ | auto: MaterialTest.test_the_material_holds_the_evidence_and_the_users_choices；实测：agent 引用了用户说过的「一块清理了吧」（m7）才关 10-29 的后台，开着的模拟器一台没列 |
| AC-11 | [核] | agent 正在处理，或 24 小时内处理过一次告急 | 用户看封面，或收到「agent 处理了告急」通知 | • 封面圆环下面一行：「Codex 正在处理告急 ›」或「Codex 17:40 处理过告急 ›」，鼠标停上去看它做了什么；后台正在搬东西时先显示进度；<br>• 点这一行、点通知本身或通知上的「看对话」：Codex 在它的 App 里打开这段对话，Claude 在终端里接着这段对话（还在后台跑时先说一声，跑完再点）；<br>• 有东西等你确认时，通知上多一个「去确认」，点开就是「等你确认」 | CLI后端、mac原生 | ⬜ | |
| AC-12 | [核] | 一台只装了 Claude 桌面 App、没在终端登录过 Claude Code 的 Mac | 第一次告急 | 退回「交给 agent」的通知，并且一天一次提醒「还差一步：登录一次 Claude Code」；点一下打开终端，在浏览器里授权一次，以后告急它就能自己来。`emergency status` 写明现在谁能自己来、还差什么 | CLI后端、mac原生 | ⬜ | |
| AC-10 | [边] | agent 处理完，或 30 分钟还没结束 | `rgs emergency done --summary`，或到时间 | 记下这次告急的结论；到时间就停掉这次运行，agent 已经记下的决定照样处理 | CLI后端 | ✅ | auto: RunnerTest.test_a_run_that_overstays_is_stopped_and_its_decisions_still_apply、RunnerTest.test_a_run_that_breaks_clears_itself_and_hands_over |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-31 | 新建（RAW-18～22）。推翻 10-30 决策卡 A01「后台不无人值守地跑 agent」：告急时自己启动，平时照旧 | [需求变更] |
| CH-02 | 2026-10-31 | 实现时查明两件事：<br>• Claude 桌面 App 的登录不给后台的 Claude Code 用（后台跑会提示「Not logged in」），桌面的定时任务也只能由 agent 自己建，所以 Claude 要用户在终端登录一次；<br>• Codex 在沙盒里跑时只能往这次运行的目录写，所以 agent 的命令只记下决定，真正的动作由后台在 agent 结束后做（AC-06、AC-07） | [实现引入] |
| CH-03 | 2026-10-31 | 本机真跑时发现并修好（RAW-23、RAW-24：Claude 卡，用户说「关掉」两个截屏流）：<br>• 材料里外接盘路径不是文字，第一轮写文件时崩了；现在只存文字，跑崩时立刻清掉这一轮、退回「交给 agent」的通知（AC-10）；<br>• 逐个 du 任务目录要 10 分钟，改用最近一次体检的大小，各仓库并行查（AC-05）；<br>• 父进程已退出、开了一两天的 `baguette stream` 截屏流也算残留后台；<br>• test-journal 的归档服务父进程也是 launchd，差点被当成残留，现在 launchd 管着的服务一律不算（AC-06） | [实现引入] |
| CH-04 | 2026-10-31 | 加 AC-11、AC-12（RAW-26～28）：封面一行和通知直达 agent 的对话；为了别人装上就能用，查清只在这台 Mac 上成立的几处：<br>• Claude Code 是否登录改用它自己的 `claude auth status`；<br>• 老版本 Claude 桌面 App 里 Claude Code 的另一种放法、Codex App 的几种安装位置都认；<br>• Codex 不认识「来源」字段时去掉它重开，照样能跑 | [需求变更] |
