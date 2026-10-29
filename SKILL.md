---
name: mac-dev-resource-guard
description: Mac 开发者的磁盘空间管家（资源看板）。安装资源看板，按用户定的空间目标出计划，处理需要取舍和判断的部分；各任务共享构建缓存、用好外接盘；通用清理借力 Mole。用于：磁盘或内存吃满、开发缓存增长太快、用户发来本仓库链接要安装、用户粘贴了「用 mac-dev-resource-guard 这个 skill 帮我看一下磁盘空间」。
---

# 资源看板（mac-dev-resource-guard）

资源看板是菜单栏里的一张封面：还剩多少、在帮你管什么、有什么要你拍板。后台脚本负责规则内的事（共享缓存、清理至少 7 天未用的编译中间目录）；**为了凑够用户的空间目标、需要取舍和判断的部分，由你（用户的 agent）来做**，每一项都先跟用户确认。命令都在 `~/.local/bin/resource-guard-service`，下文简写为 `rgs`，可以先定义：`rgs() { ~/.local/bin/resource-guard-service "$@"; }`。

## 1. 用户发来仓库链接、要安装时

1. **先看是新装还是升级**：`test -x ~/.local/bin/resource-guard-service` 成立就是升级。**升级什么都不用问**，直接运行下面的安装命令（不带变量），装完告诉用户版本和当前设置（`rgs target` 看目标）。
2. **新装只问一句**（你的命令行没有终端，安装脚本问不到用户）。先算建议目标（磁盘约 20%，取整到 10 GB）：
   ```bash
   python3 -c "import shutil; t = shutil.disk_usage('/').total / 2**30; print(max(20, round(t * 0.2 / 10) * 10))"
   ```
   然后用一句话问，不要拆成几个选项、不要单独问外接盘：
   > 我会装好资源看板，默认打开共享构建缓存（各任务共用一份工具缓存，并打开 Xcode 编译缓存，会在 ~/.zshenv 加一段可一键还原的设置；插着外接盘时，缓存和构建产物也放过去），内置盘至少留 X GB（约磁盘的 20%）。可以吗？

   用户说可以，就带上 `RESOURCE_GUARD_ENABLE=yes RESOURCE_GUARD_TARGET=X`；用户只改了数字，就用他的数字；用户不想开缓存，就写 `RESOURCE_GUARD_ENABLE=no`。安装会下载源码、在本机编译、装好 App、后台任务和这份 skill，装完就已开好：
   ```bash
   curl -fsSL https://raw.githubusercontent.com/ruanjianjian/mac-dev-resource-guard/main/install.sh | RESOURCE_GUARD_ENABLE=yes RESOURCE_GUARD_TARGET=X bash
   ```
   缺 Xcode 命令行工具时脚本会停下并弹出系统安装窗口，让用户装好再跑。后台正在整理时，安装会自己等它做完。
3. 记下你是谁，以后看板要交接时会打开你：`rgs agent-app claude`（Claude Code）或 `rgs agent-app codex`（Codex）。
4. 安装输出提示 macOS 可能问「资源看板」能否访问外接盘时，转告用户点「允许」。以后要改目标或单独开关某一项，用户在面板「帮你管着」里点，或告诉你（`rgs target 90`）。
5. 用户不用外接盘、又要求全程后台时：同意后照样带 `RESOURCE_GUARD_ENABLE=yes` 安装，再 `python3 ~/Applications/资源看板.app/Contents/Resources/backend.py checkup-off --ids external`。不打开面板、不重启现有终端或 IDE。
6. 自动清理固定为 `auto_scope=managed-caches`：只删至少 7 天没用的编译中间缓存，不自动删任务、构建产物或模拟器。开了外接盘、空间低于提醒线时，后台会把闲置满 3 天、改动都已提交并推送的任务整个挪过去（不删，原路径留链接）。关闭自动清理的选择会保留，不要手动改规则。
7. 按第 2 节出计划、处理。

## 2. 出计划、处理

`rgs plan --json` 输出计划：目标、还差多少、三段（`auto` 会自动做的 / `ask` 等你点头的 / `judge` 需要判断的）、全做完能到多少、还差时的建议。没体检过时会提示先体检。

- **auto**：交给后台脚本，不要自己动手。只告诉用户「这部分会自动做，约 N GB」。
- **ask**：逐项讲清楚是什么、为什么能删、会失去什么，用户同意的才执行。执行走看板的确认命令（执行前会逐项复核）：
  ```bash
  B=~/Applications/资源看板.app/Contents/Resources/backend.py
  python3 $B pending-preview            # 拿到 token 和各项 id
  python3 $B pending-clean --token <token> --ids <id1,id2>
  ```
- **judge**：工具判断不了的，比如「一个任务里有 3 份 Xcode 构建产物」「一个大任务最近还动过」。你去看证据再给建议：
  - 多份构建产物：计划只列一周以上没用过的副本（一周内用过的和每个任务最新的一份不列）。你再看各自对应哪个工作区、构建脚本现在指向哪一份；只建议删明显没人用的，**一周内用过的不要建议删**。用户同意后 `rgs prune <目录>`（只删可重建产物名单里的目录，正在用的会被拒绝）。
  - 想收尾的任务：先确认所有仓库都已提交并推送，再问用户，同意后用 `git worktree remove` 收尾。有未提交或未推送的改动绝不删。
  - 想让构建以后不再各编一份：可以改项目的构建脚本（例如让 DerivedData 指向共享位置）。**先把改动给用户看，同意后在新分支上改**，按项目自己的流程提交。
- 资源看板自己的设置（目标、闲置天数、开关）可以直接改，改完告诉用户。
- 全做完仍不够时，如实告诉用户还差多少，给出建议（收尾不用的任务、换更大的外接盘），**不要为了凑数放宽规则**。

## 3. 看板叫你来时

出意外时（脚本做完仍低于目标，或某处一天涨了 10 GB 以上），看板会发通知，用户点通知后会把一句话粘给你。照第 2 节处理，并说明这次多出来的是什么。

## 4. 通用清理：借力 Mole

系统缓存、日志、App 残留、安装包、找大文件这些通用清理，交给 [Mole](https://github.com/tw93/Mole)（`mo`）。

- 没装就先问用户，同意后安装：`brew install mole`，或照 Mole README 的脚本安装。
- **每次调用 Mole 前先运行 `rgs mole-guard`**：把资源看板管的地方（共享缓存、任务目录、外接盘目录）写进 Mole 的白名单，免得它清掉共享缓存。
- 先预演给用户看：`mo clean --dry-run`、`mo purge --dry-run`、`mo installer --dry-run`；找大文件用 `mo analyze --json <目录>`。用户确认后再去掉 `--dry-run` 执行。
- 用户在看板里关掉共享构建缓存时，运行 `rgs mole-guard remove` 撤掉白名单里资源看板加的那一段。

## 5. 内存

只回收有闲置证据的开发后台（资源看板已有，`dev-memory status --json` 查看）。用户说卡、内存紧时，用 `mo status` 看谁在占用、给建议。**不做「一键释放内存」**：不 purge、不删 swap、不 killall、不强杀用户的 App、容器、数据库或调试器。

## 不变的边界

1. 空间目标只决定何时整理、整理到多少，永远不拦编译、装依赖或跑测试。
2. 正在用、被保护（pin、lease）、有未提交或未推送改动、状态判断不了的，一律保留。
3. 删除和挪动只走看板的命令、`rgs prune` 或 Mole（先预演、用户确认）；不直接 `rm -rf`。
4. 模拟器不放外接盘；删除含登录状态的模拟器只走「等你确认」或用户在对话里点头。
5. 外接盘拔掉时，用到它的构建会报错，不要在内置盘另建一份去「修好」它；告诉用户插回即可。
6. 汇报时分清：目录大小、实际腾出的空间、还差多少，不混为一谈。

更多：[README](README.md) · [装好就省心](docs/epics/one-click-setup/brief.md) · [安装与兼容入口](references/install.md) · [内存](references/memory.md) · [缓存复用](references/cache.md) · 测试：`python3 scripts/verify.py`。
