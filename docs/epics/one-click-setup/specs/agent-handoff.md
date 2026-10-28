# Spec: 交给本地 agent v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-22～30。讨论过程：决策卡 https://claude.ai/artifact/KYuFV6eBFYkwm4XCrb3aJf（三轮 16 张，用户 2026-10-28 看完）。

## 意图

用户会把 GitHub 链接发给自己的 agent（Claude Code 或 Codex）来安装，skill 随安装一起到位（RAW-26、A01）。装完 agent 主动问要不要设空间目标；设了就按 skill 出计划并处理：脚本本来做得好的自动档照旧由脚本做，为了凑够目标、需要取舍的部分（「等你点头」「需要判断」）整个交给 agent，在对话里跟你确认后执行（RAW-24～25）。之后只有出了意料之外的情况，看板才叫起 agent（B02、B03）。通用清理借力高星开源项目 Mole，资源看板管的地方写进它的白名单（C01～C05）。

## 非目标

- 不在后台悄悄运行 agent（B02 选了打开 App）。
- agent 不直接删文件：删和挪只走看板的命令或 Mole 的预演后确认，复核规则不变。
- 不做「一键释放内存」（C05）。
- 资源看板的安装程序不装 Mole（C02）。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 用一条命令或 agent 安装资源看板 | 安装完成 | 随附的 skill 装到 Codex 和 Claude Code 都能用的位置，升级时一起更新；本机已有的旧 skill 有本地改动时保留并提示，没有改动时先备份再替换 | CLI后端 | ✅ | auto: SkillInstallTest（干净的旧克隆备份后替换、Claude 链接、有改动的保留） |
| AC-02 | [核] | 用户把仓库链接发给 agent | agent 读 README | README 里有一段写给 agent 的安装步骤：怎么装、装完问用户要不要设目标、怎么读计划 | CLI后端 | ✅ | README「让你的 agent 来装」与 SKILL.md 第 1 节 |
| AC-03 | [核] | 装好了，用户说了目标 | agent 按 skill 工作 | agent 写入目标、读计划；自动档交给脚本；「等你点头」「需要判断」两段逐项判断，在对话里说明理由和会失去什么，用户同意后用看板命令执行 | CLI后端 | ⚠️ | SKILL.md 第 2 节写明流程；执行用的命令都有测试（pending-clean、prune、target、agent-app）；没有让真实的 agent 从头走一遍 |
| AC-04 | [核] | 出现意料之外的情况 | 看板提醒 | 发通知「有新占用需要判断」；点通知后，把要对 agent 说的话复制到剪贴板，并打开 agent App（Claude 或 ChatGPT 里的 Codex） | mac原生 | ⚠️ | auto: BackendPlanTest（plan-handoff 返回要说的话和 App 路径）；复制并打开 App 的效果要看屏幕，未截图 |
| AC-05 | [核] | 脚本能做的都做完仍低于目标，或某类、某个任务一天涨了 10 GB 以上且规则管不到 | 每轮检查 | 触发 AC-04 的提醒，每天最多一次；其他情况不叫 | CLI后端 | ✅ | auto: test_agent_is_woken_once_a_day_for_the_unexpected_only、test_fast_growth_is_noticed_against_a_daily_baseline |
| AC-06 | [核] | agent 要改东西 | 改资源看板设置 / 改项目构建脚本 | 设置直接改并告诉用户；项目脚本先把改动给用户看，同意后在新分支上改 | CLI后端 | ✅ | auto: test_agent_may_remove_a_build_copy_inside_a_task_only（只删任务里的可重建产物，正在用的拒绝）；改项目脚本先给用户看、在新分支改，写在 SKILL.md |
| AC-07 | [核] | 本机装了 Mole | agent 调用 Mole 清理 | 先把资源看板管的地方（共享缓存、任务目录、外接盘目录）追加进 Mole 的白名单，只追加、带标记；先预演给用户看，确认后再执行；资源看板「还原」时撤掉自己加的行；没装 Mole 时先问用户要不要装 | CLI后端 | ✅ | auto: MoleGuardTest（首次写入保留 Mole 默认白名单、重复写只有一段、撤销只去掉自己那段、读不到默认值就拒绝）；实测 [横评](../tool-eval.md)：不加白名单时 Mole 会清掉 T9 上的共享缓存（Xcode 15.3 GB、npm 1.1 GB 等） |
| AC-08 | [边] | 用户说电脑卡、内存紧 | agent 处理 | 只回收有闲置证据的开发后台（资源看板已有）；系统层面用 Mole 的状态面板看占用、给建议，不做「一键释放内存」 | CLI后端 | ✅ | SKILL.md 第 5 节 |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-28 | 新建（RAW-22～30） | [需求变更] |
| CH-02 | 2026-10-28 | C03 按用户要求先做横评（RAW-29、[横评报告](../tool-eval.md)），结论维持只接 Mole；同时发现改过名的 Xcode 构建文件夹，计划和 agent 的删除命令都改为认「名字里带 DerivedData」 | [实现引入] |
