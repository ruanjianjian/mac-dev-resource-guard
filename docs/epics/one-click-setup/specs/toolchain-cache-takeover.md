# Spec: 接管开发工具缓存 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-01、RAW-04、RAW-08。讨论过程：决策卡 https://claude.ai/artifact/B2QaPqf6Fc41G2LptKy1ez。

## 意图

各开发工具的下载缓存、编译缓存散在主目录各处，全占着内置盘；Xcode 编译缓存默认没开（RAW-01）。打开「共享构建缓存」后：工具找出本机装了哪些开发工具，插着外接盘时把它们的缓存搬到外接盘上一个由看板管理的目录，原位置留链接，各工具照常使用；同时全局打开 Xcode 编译缓存，同一个任务里切分支、清空重编时更快（RAW-04、RAW-08）。改过的设置随时一键还原。

## 非目标

- 不承诺多个任务共用 Xcode 编译结果（决策卡 R02 实测换目录几乎用不上）。
- 不改项目自己的构建脚本和配置。
- pnpm 包仓库、uv 缓存不搬到外接盘：它们要和项目在同一块盘上才能共享文件。
- 还原时不把缓存搬回内置盘：缓存能重新下载和生成。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 打开缓存共享，插着外接盘 | 下一轮后台检查 | 本机装了的工具（Xcode 构建缓存、CocoaPods、Flutter、npm、yarn、Go、Gradle、Cargo、Homebrew、pip）的缓存搬到外接盘上看板管理的目录，原位置留链接；工具照常可用；看板列出每个工具在哪、多大 | CLI后端、mac原生 | ✅ | 实测（实测见任务目录 one-click-evidence/e2e-one-click.log 第 1、3 步）：npm、Go（只读模块缓存）、Cargo 缓存真实 ditto 到 T9，原处成链接，读链接与 T9 副本逐字节一致；后台一轮搬走 npm、Flutter 缓存并记入「自动记录」（auto-record-cache-moves.png）；auto: test_local_caches_move_to_the_drive_and_tools_keep_their_paths、TickTest |
| AC-02 | [核] | 某个工具正在运行（例如 Xcode、xcodebuild、go build、pod install） | 轮到搬它的缓存 | 这一轮跳过它，下一轮再试；搬迁过程中文件有变化就放弃，原缓存不动 | CLI后端 | ✅ | auto: test_a_running_tool_is_skipped_until_next_round；搬迁中变化即放弃沿用 test_tiers.test_change_during_copy_aborts_and_leaves_no_copy（同一 move_dir） |
| AC-03 | [边] | 缓存位置已是用户自己做的链接，或工具已改过缓存位置 | 接管 | 在外接盘上的认作已接管，不重复搬、不改用户的设置；还原时也不动它们 | CLI后端 | ✅ | auto: test_user_links_and_custom_locations_are_adopted_not_touched；实测：本机 DerivedData、CocoaPods、pub、npm 已是指向 T9 的链接，Go 用 go env 改过位置，体检全部认作已在外接盘、不动 |
| AC-04 | [边] | 有 pnpm 包仓库或 uv 缓存 | 接管 | 不搬，写明原因「要和项目在同一块盘上才能共享文件」 | CLI后端 | ✅ | auto: 同 AC-01 用例（pnpm 留在原地并写明原因）；实测：本机 pnpm 包仓库显示「留在内置盘」 |
| AC-05 | [核] | 打开 Xcode 编译缓存 | 之后在新开的终端或 Xcode 里构建 | 自动带上编译缓存设置，同一任务清空重编时用上缓存；用户已有自己的全局 Xcode 设置文件时不覆盖，提示冲突 | CLI后端 | ✅ | 实测（e2e 第 2 步）：隔离主目录打开后，新开 zsh 带上 XCODE_XCCONFIG_FILE，xcodebuild 显示 COMPILATION_CACHE_ENABLE_CACHING = YES，第二次清空重编 30 次编译全部命中缓存；auto: test_enable_reaches_new_shells_and_apps_and_disable_undoes_it、test_someone_elses_global_settings_are_not_overwritten、test_periodic_job_puts_the_setting_back_after_logout |
| AC-06 | [核] | 已经接管 | 在看板点「还原」 | 去掉看板做的链接，各工具回到默认位置（缓存从空开始重建），删掉外接盘上的缓存副本，关掉 Xcode 编译缓存；之后新开的终端和 Xcode 不再带编译缓存设置 | CLI后端、mac原生 | ✅ | 实测：e2e 第 1 步还原后 ~/.npm 为普通空目录、T9 副本删除；第 2 步关闭后新终端不再带设置；auto: test_turning_caches_off_restores_and_switches_xcode_off |
| AC-07 | [边] | 没插外接盘 | 打开缓存共享 | 缓存留在原地，只统一显示和清理；Xcode 编译缓存照常打开 | CLI后端 | ✅ | auto: test_caches_without_a_drive_stay_put_but_xcode_cache_turns_on |
| AC-08 | [核] | 打开缓存共享，插着外接盘；某个任务目录里有自己的构建产物：Xcode 的（名字带 DerivedData，例如构建脚本用 -derivedDataPath 指到任务目录里），或 SwiftPM 等工具用 CACHEDIR.TAG 标明的构建输出（例如每轮测试一份 `swift build --scratch-path`）；在某个仓库里面的不算 | 下一轮后台检查 | 这份构建产物没在构建、30 分钟没动时，挪到外接盘「dev-resource-guard/builds/<任务>/」，原路径留链接，之后的构建照常写到原路径（实际在外接盘）；每轮挪一份，内置盘低于提醒线时每轮最多挪 10 份；正在构建或刚构建完的等下一轮；不删任何东西；关掉缓存共享时这些构建产物留在外接盘、链接不动；计划把它们算进「会自动做的」，已挪走的不再算内置盘占用 | CLI后端 | ✅ | auto: test_one_click.TaskBuildTest（安静的先挪、一轮一份、链接后内容可读、构建中 / 5 分钟内构建过的不动、还原后留在外接盘、计划计入且挪后不再计）；起因：本机 10-29 四个任务各自的 DerivedData 共约 33 GB 留在内置盘；auto: test_build_output_tagged_with_cachedir_tag_moves_too（第五层的 SwiftPM 构建输出挪走、标记不对的不动、仓库里的不动）、TickTest.test_below_the_warning_line_several_build_folders_move_per_round；起因：本机 10-30 一个任务里 46 份 SwiftPM 构建输出共 26 GB，内置盘跌到 15 GB |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-28 | 新建（RAW-01、RAW-04、RAW-08） | [需求变更] |
| CH-02 | 2026-10-29 | 任务目录里各自的 Xcode 构建产物也挪到外接盘（AC-08）；RAW-37、RAW-38：用户让按最正确的方式做 | [需求变更] |
| CH-03 | 2026-10-30 | 资源管家没自动处理（RAW-79）：只认名字带 DerivedData 的构建产物，SwiftPM 等工具标了 CACHEDIR.TAG 的构建输出一直留在内置盘；现在一并挪到外接盘，在仓库里面的不挪（换成链接会出现在 git status 里）；低于提醒线时每轮最多挪 10 份（AC-08） | [缺陷] |
