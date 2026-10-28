# Spec: 空间监控与扩大清理范围 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-01、RAW-06、RAW-07。讨论过程：决策卡 https://claude.ai/artifact/B2QaPqf6Fc41G2LptKy1ez。

## 意图

用户不用打开面板，也该知道空间够不够：菜单栏图标旁直接显示剩余空间，按颜色提醒（RAW-07）。监控和清理扩大到开发中常见的占用：Xcode 的设备支持文件和归档、废纸篓、下载文件夹里的大安装包；Docker、OrbStack 只显示大小并提醒（RAW-06）。会丢东西的仍进「待确认」，用户勾选后才做。

## 非目标

- 不替用户删 Docker、OrbStack 的镜像。
- 不扫描下载文件夹里的安装包以外的文件。
- 不改 macOS 的隐私设置：读不到废纸篓时只说明，不绕过。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | App 在菜单栏 | 平时 | 图标旁显示内置盘剩余空间（如「21G」）；低于提醒线变橙色，低于危险线变红色；每 15 秒更新 | mac原生 | ⚠️ | auto: App 自检 free-space colour levels（80/50/21 GB 对应 正常/橙/红）；本机已装 1.5，菜单栏实物未截图（需要接管屏幕） |
| AC-02 | [核] | 下载文件夹里有 7 天没动、超过 200 MB 的安装包（dmg、pkg、xip、iso），且没有挂载、没在用；用户打开过看板，允许过读取下载文件夹 | 后台检查 | 进「待确认」，写明大小和「会失去：安装包文件，要用时得重新下载」；勾选后才删 | CLI后端、mac原生 | ✅ | auto: test_old_big_installers_are_proposed_new_small_or_mounted_are_not、test_downloads_is_untouched_in_the_background_until_read_from_the_panel、test_confirmed_installer_is_deleted_and_trash_goes_through_finder |
| AC-03 | [核] | 废纸篓超过 1 GB 且能读到大小 | 后台检查 | 进「待确认」，项目叫「清空废纸篓」；读不到大小时只在体检页说明原因，不进待确认 | CLI后端 | ✅ | auto: test_trash_only_when_readable_and_big；实测：本机废纸篓读不到，体检页写明原因、未进待确认 |
| AC-04 | [核] | Xcode 的设备支持文件 30 天没用、归档是 90 天前的 | 后台检查 | 进「待确认」；归档写明会失去这个版本的安装包和查崩溃用的符号文件 | CLI后端 | ✅ | auto: test_device_support_and_archives_by_age |
| AC-05 | [核] | 装了 Docker 或 OrbStack | 体检 | 显示它们的磁盘镜像大小和「请到 Docker 里清理」，工具不删 | CLI后端、mac原生 | ✅ | auto: test_container_disk_images_are_only_reported（OrbStack）；Docker 走 docker system df，本机没装 Docker，未实跑 |
| AC-06 | [边] | 新增的几类进了待确认 | 发通知 | 计入「有 N 项清理等你确认」，沿用每天最多一次的节流 | CLI后端 | ✅ | 新增几类和原有待确认项进同一列表，计数与节流沿用 ask_tier.maybe_notify（test_ask_tier 的节流用例） |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-28 | 新建（RAW-01、RAW-06、RAW-07） | [需求变更] |
| CH-02 | 2026-10-28 | [实现引入] 本机实装发现：后台读取下载文件夹会让 macOS 弹出访问询问，后台检查卡在那里等回答。改为用户打开看板体检时才第一次读取下载文件夹（系统当场询问一次）；读过之后后台检查才会看下载里的安装包 | [实现引入] |
