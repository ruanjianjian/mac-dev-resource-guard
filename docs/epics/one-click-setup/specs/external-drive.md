# Spec: 外接盘放缓存和闲置任务 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-01、RAW-08、RAW-09、RAW-11。讨论过程：决策卡 https://claude.ai/artifact/B2QaPqf6Fc41G2LptKy1ez。

## 意图

有外接盘时，把能放的都放上去：各工具的缓存全放，闲置满 3 天的任务目录也挪过去（RAW-08）。外接盘可能被拔掉：拔掉后，用到它的构建直接报错，不在内置盘另建缓存（与 1.4 项目入口 AC-05 一致，RAW-11），同时马上提醒用户（RAW-09）。

## 非目标

- 拔盘后不自动切换到内置盘，也不建备用缓存（RAW-11）。
- 模拟器不放外接盘。
- 不改 macOS 的隐私设置；访问外接盘的授权由用户在系统弹窗里点「允许」。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 在体检页选了外接盘 | 开启 | 缓存放到外接盘（见 toolchain-cache-takeover AC-01），闲置满 3 天的任务目录挪过去（沿用 1.4 tiered-auto-cleanup AC-01）；新建的外接目录都在「<盘>/dev-resource-guard」下，已手配的外接目录沿用 | CLI后端 | ✅ | auto: test_scan_lists_space_and_apply_turns_everything_on_once（外接目录在 <盘>/dev-resource-guard 下）、test_hand_written_settings_are_kept_and_not_duplicated（手配外接目录沿用）；闲置任务搬迁沿用 1.4 tiered AC-01 |
| AC-02 | [核] | 用了外接盘 | 外接盘被拔掉 | 几秒内发系统通知「外接盘 T9 已断开」，写明哪些缓存、几个任务暂时不能用，用到它们的构建会报错，插回即可；不在内置盘另建缓存 | mac原生 | ⚠️ | 实现：App 监听「盘已断开」后调 checkup-status 并发通知；e2e 第 4 步验证通知文案「外接盘「…」已断开 / 暂时不能用：npm；1 个任务在上面…」；没有在本机真的拔 T9 |
| AC-03 | [核] | 外接盘没连接 | 打开体检页 | 顶部提示外接盘未连接，列出受影响的缓存和任务；搬迁和接管暂停，其他清理照常 | mac原生、CLI后端 | ✅ | ui: 实测见任务目录 one-click-evidence/checkup-4-unplugged.png；auto: test_unplugged_drive_is_reported_with_what_it_takes_away |
| AC-04 | [边] | 外接盘没连接 | 后台检查 | 不在 /Volumes 下误建同名目录，不运行任何会让 git 丢失工作区记录的命令 | CLI后端 | ✅ | auto: test_unplugged_or_full_drive_moves_nothing（/Volumes 下不建目录）；e2e 第 4 步同样核对 |
| AC-05 | [边] | 外接盘剩余空间不够 | 要搬缓存或任务 | 跳过，记录写「外接盘空间不够」 | CLI后端 | ✅ | auto: test_unplugged_or_full_drive_moves_nothing（空间不够 → 「外接盘空间不够」） |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-28 | 新建（RAW-01、RAW-08、RAW-09、RAW-11） | [需求变更] |
