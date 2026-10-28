# Spec: 一条命令安装 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-01、RAW-02、RAW-12。讨论过程：决策卡 https://claude.ai/artifact/B2QaPqf6Fc41G2LptKy1ez。

## 意图

现在装资源看板要先克隆仓库、再跑安装脚本，看不懂终端的同学会卡住（RAW-01「不够傻瓜」）。改成复制一行命令粘贴到终端：它自己下载源码、在本机编译、装好 App 和后台任务。装完不弹面板，App 第一次启动后在后台做一次体检，发一条「体检完成」通知，点开就是体检页（RAW-02）。以后升级也是同一行命令。

## 非目标

- 不做需要苹果付费开发者账号的签名、公证安装包（RAW-12）。
- 不替用户安装 Xcode 或命令行工具：缺了就停下，请系统弹出安装提示。
- 不经 Homebrew 分发。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 新 Mac 装好了命令行工具，没克隆过仓库 | 在终端粘贴安装命令 | 自动下载源码、在本机编译并装好 App 和后台任务；最后打印「安装完成」并说明稍后会收到体检通知；面板不弹出 | CLI后端 | ✅ | 实测：本机 `curl …/install.sh \| RESOURCE_GUARD_REF=claude/guard-1.5-one-click bash` → 打印「安装完成…稍后会收到「体检完成」通知」，App 1.5.0，面板未弹出；auto: test_one_click.InstallScriptTest.test_downloads_swaps_source_and_runs_the_installer |
| AC-02 | [核] | 已经装过旧版本 | 再跑一次同一条命令 | 升级到最新版；设置、保护记录、已挪到外接盘的任务目录保持不变 | CLI后端 | ✅ | 实测：同一条命令在本机连跑 3 次（1.4→1.5→两次修复），auto-cleanup.json（自动档、任务根、外接目录）原样保留，worktrees 下 15 个外接盘链接保留；auto: 同上用例第二次安装 |
| AC-03 | [边] | 不是 macOS，或缺命令行工具、Python 3 | 跑安装命令 | 停下并说明缺什么、怎么装（缺命令行工具时弹出系统安装提示），不留下半套安装 | CLI后端 | ⚠️ | 实现：非 macOS / 缺 Python / 缺命令行工具时报错并调用 xcode-select --install；没有在缺工具的机器上实跑 |
| AC-04 | [边] | 下载中断或源码包损坏 | 跑安装命令 | 报「下载失败」，已装的旧版本照常可用 | CLI后端 | ✅ | auto: test_one_click.InstallScriptTest.test_broken_download_keeps_what_was_installed（坏包 → 「下载失败或源码包不完整」，旧源码原样） |
| AC-05 | [核] | 安装刚完成 | App 第一次启动 | 后台做一次体检，完成后发一条通知「体检完成」，写明可腾出约多少空间；点通知打开体检页；同一版本只发一次 | mac原生 | ⚠️ | 实测：本机安装后首次体检后台跑完（17:57:22→17:58:55），checkup.json 记下 notified_version=1.5.0-8，即已调用系统通知；通知横幅与点击打开体检页需要看屏幕，未截图 |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-28 | 新建（RAW-01、RAW-02、RAW-12） | [需求变更] |
