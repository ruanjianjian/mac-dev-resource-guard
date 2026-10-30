# Spec: 交给本地 agent v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-22～30、RAW-85～86。讨论过程：决策卡 https://claude.ai/artifact/KYuFV6eBFYkwm4XCrb3aJf（三轮 16 张，用户 2026-10-28 看完）。

## 意图

用户会把 GitHub 链接发给自己的 agent（Claude Code 或 Codex）来安装，skill 随安装一起到位（RAW-26、A01）。装完 agent 主动问要不要设空间目标；设了就按 skill 出计划并处理：脚本本来做得好的自动档照旧由脚本做，为了凑够目标、需要取舍的部分（「等你点头」「需要判断」）整个交给 agent，在对话里跟你确认后执行（RAW-24～25）。之后只有出了意料之外的情况，看板才叫起 agent（B02、B03）。通用清理借力高星开源项目 Mole，资源看板管的地方写进它的白名单（C01～C05）。

## 非目标

- 不在后台悄悄运行 agent（B02 选了打开 App）。
- 不替用户自动发送那句话：两个 App 的链接都只填进输入框，由用户按回车（RAW-86 选了「话已填好」；Codex 点一下就开干的做法没做）。
- agent 不直接删文件：删和挪只走看板的命令或 Mole 的预演后确认，复核规则不变。
- 不做「一键释放内存」（C05）。
- 资源看板的安装程序不装 Mole（C02）。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 用一条命令或 agent 安装资源看板 | 安装完成 | 随附的 skill 装到 Codex 和 Claude Code 都能用的位置，升级时一起更新；本机已有的旧 skill 有本地改动时保留并提示，没有改动时先备份再替换 | CLI后端 | ✅ | auto: SkillInstallTest（干净的旧克隆备份后替换、Claude 链接、有改动的保留） |
| AC-02 | [核] | 用户把仓库链接发给 agent | agent 读 README | README 里有一段写给 agent 的安装步骤：怎么装、装完问用户要不要设目标、怎么读计划 | CLI后端 | ✅ | README「让你的 agent 来装」与 SKILL.md 第 1 节 |
| AC-03 | [核] | 装好了，用户说了目标 | agent 按 skill 工作 | agent 写入目标、读计划；自动档交给脚本；「等你点头」「需要判断」两段逐项判断，在对话里说明理由和会失去什么，用户同意后用看板命令执行 | CLI后端 | ✅ | 实测（RAW-31～33）：用户把交接那句话粘给 Claude Code，agent 照 skill 读计划；「等你点头」的 iOS 27.1 运行时讲明会失去什么，用户同意后经 pending-preview / pending-clean 删除，可用空间 45 → 54 GB；「需要判断」的 9 份 Xcode 构建副本逐份查了最近构建时间和对应工作区，用户留言「一周内有还在用的都别删」与勾选矛盾，当面澄清后一份未删 |
| AC-04 | [核] | 出现意料之外的情况，或用户在计划页点「交给 agent ›」 | 看板提醒，用户点通知或按钮 | 发通知「有新占用需要判断」；点通知或按钮后，agent App 打开一个新对话，要说的话已经填在输入框里，用户按回车发送；剪贴板不动。Claude 打开 Claude Code 的新会话，ChatGPT 里的 Codex 打开新对话。本机没有 agent App，或打不开这个链接（比如只装了普通 ChatGPT），才退回复制到剪贴板并打开 App | CLI后端、mac原生 | ✅ | auto: BackendPlanTest.test_handoff_opens_a_new_conversation_with_the_words_typed_in（链接里的话解出来与原话逐字相同；旧代码上失败）；ui: 2026-10-30 本机（App 为分支 claude/handoff-deep-link）真实打开 Codex 链接：ChatGPT 里的 Codex 开了新对话，整句话在输入框里、未发送（task-dir one-click-evidence/handoff/codex-prefill.jpg）；真实打开 Claude 链接：Claude 日志显示切到新的 Code 会话页（setFocusedSession null）、无报错；输入框里的字未截到图（Claude 自己的窗口不在截图里），只填不发以官方文档为准 |
| AC-05 | [核] | 脚本能做的都做完仍低于目标，或某类、某个任务一天涨了 10 GB 以上且规则管不到 | 每轮检查 | 触发 AC-04 的提醒，每天最多一次；其他情况不叫 | CLI后端 | ✅ | auto: test_agent_is_woken_once_a_day_for_the_unexpected_only、test_fast_growth_is_noticed_against_a_daily_baseline |
| AC-06 | [核] | agent 要改东西 | 改资源看板设置 / 改项目构建脚本 | 设置直接改并告诉用户；项目脚本先把改动给用户看，同意后在新分支上改 | CLI后端 | ✅ | auto: test_agent_may_remove_a_build_copy_inside_a_task_only（只删任务里的可重建产物，正在用的拒绝）；改项目脚本先给用户看、在新分支改，写在 SKILL.md |
| AC-07 | [核] | 本机装了 Mole | agent 调用 Mole 清理 | 先把资源看板管的地方（共享缓存、任务目录、外接盘目录）追加进 Mole 的白名单，只追加、带标记；先预演给用户看，确认后再执行；资源看板「还原」时撤掉自己加的行；没装 Mole 时先问用户要不要装 | CLI后端 | ✅ | auto: MoleGuardTest（首次写入保留 Mole 默认白名单、重复写只有一段、撤销只去掉自己那段、读不到默认值就拒绝）；实测 [横评](../tool-eval.md)：不加白名单时 Mole 会清掉 T9 上的共享缓存（Xcode 15.3 GB、npm 1.1 GB 等） |
| AC-08 | [边] | 用户说电脑卡、内存紧 | agent 处理 | 只回收有闲置证据的开发后台（资源看板已有）；系统层面用 Mole 的状态面板看占用、给建议，不做「一键释放内存」 | CLI后端 | ✅ | SKILL.md 第 5 节 |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-28 | 新建（RAW-22～30） | [需求变更] |
| CH-02 | 2026-10-28 | C03 按用户要求先做横评（RAW-29、[横评报告](../tool-eval.md)），结论维持只接 Mole；同时发现改过名的 Xcode 构建文件夹，计划和 agent 的删除命令都改为认「名字里带 DerivedData」 | [实现引入] |
| CH-03 | 2026-10-28 | 第一次真实走 skill：用户定下「一周内用过的构建副本不删」（RAW-32～33） | [需求变更] |
| CH-04 | 2026-10-30 | 交给 agent 不再要用户粘贴：用 Claude（`claude://code/new?q=`）和 Codex（`codex://threads/new?prompt=`）的官方链接打开新对话、话已填好，用户只按回车；剪贴板只在打不开时兜底（RAW-85～86） | [需求变更] |
