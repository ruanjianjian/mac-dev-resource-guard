---
name: mac-dev-resource-guard
description: Mac 开发者的磁盘空间管家（资源看板）。安装资源看板，按用户定的空间目标出计划，处理需要取舍和判断的部分；各任务共享构建缓存、用好外接盘；通用清理借力 Mole。用于：磁盘或内存吃满、开发缓存增长太快、用户发来本仓库链接要安装、用户粘贴了「用 mac-dev-resource-guard 这个 skill 帮我看一下磁盘空间」。
---

# 资源看板（mac-dev-resource-guard）

资源看板是菜单栏里的一张封面：还剩多少、在帮你管什么、有什么要你拍板。后台脚本负责规则内的事（共享缓存、清理至少 7 天未用的编译中间目录）；**为了凑够用户的空间目标、需要取舍和判断的部分，由你（用户的 agent）来做**，每一项都先跟用户确认。命令都在 `~/.local/bin/resource-guard-service`，下文简写为 `rgs`，可以先定义：`rgs() { ~/.local/bin/resource-guard-service "$@"; }`。

## 1. 用户发来仓库链接、要安装时

1. 安装（会下载源码、在本机编译、装好 App、后台任务和这份 skill）：
   ```bash
   curl -fsSL https://raw.githubusercontent.com/ruanjianjian/mac-dev-resource-guard/main/install.sh | bash
   ```
   缺 Xcode 命令行工具时脚本会停下并弹出系统安装窗口，让用户装好再跑。
2. 记下你是谁，以后看板要交接时会打开你：`rgs agent-app claude`（Claude Code）或 `rgs agent-app codex`（Codex）。
3. **主动问用户空间目标**：「内置盘至少想留多少？」给一个建议值（磁盘约 10%，取整到 10 GB）。用户答了就 `rgs target 50`；不带数字的 `rgs target` 会显示当前目标和建议值。
4. 问用户要不要打开三项（共享构建缓存、自动清理、使用外接盘）。同意后：
   ```bash
   python3 ~/Applications/资源看板.app/Contents/Resources/backend.py checkup-apply --ids caches,clean,external
   ```
   打开共享构建缓存会全局打开 Xcode 编译缓存（改 `~/.zshenv` 一段带标记的设置），先讲清楚再做。macOS 问「资源看板」能否访问外接盘时，请用户点允许。
5. 不用外接盘且要求全程后台时，先 `checkup-firstrun`，再 `checkup-off --ids external`，最后 `checkup-apply --ids caches,clean`。不打开面板、不重启现有终端或 IDE。
6. 1.6.1 的新安装和升级都限制为 `auto_scope=managed-caches`；旧 `standard` 设置不会恢复自动删除任务或模拟器。关闭自动清理的选择会保留，启用前确认已有授权。不要手动改回旧版规则。
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
