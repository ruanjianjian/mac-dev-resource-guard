# Spec：资源看板按需唤起

产品文档：https://hcnqwxuq730f.feishu.cn/page/OrtGm3ZDDdm1cEaDAnVctlBBn7g

## 原始输入

[逐字原始输入](menubar-presence.raw-input.md)，本轮 RAW-01。

## 行为

启动默认只驻留菜单栏，不打开或恢复面板、不抢焦点，保持原有只读采样。菜单栏仅显示小图标，指标保留在提示和面板中。左键点击图标直接显示面板，右键打开刷新、快捷键说明和退出菜单。关闭面板后继续驻留，可重复用图标或原有全局快捷键唤起；置顶偏好只影响主动显示的窗口。再次打开已运行App也不会主动弹出面板；显式唤出统一使用图标或快捷键。

分类扫描、确认清理与活动资源保护沿用[既有规格](dashboard-actions.md)，此改动不触及后端与调度。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-06 | [核] | 安装新版App，包括曾经显示/置顶的窗口偏好 | 首次启动或退出后重启 | 面板默认隐藏、无焦点抢占，仅保留菜单栏小图标；右键可退出 | mac原生 | ✅ | auto: 实际AppDelegate启动断言window.isVisible=false、isRestorable=false、accessory与图标配置；ui: 原生NSStatusBarButton组件窗口右键菜单、选择退出；menu-bar-presence |
| AC-07 | [核] | App后台运行，面板隐藏 | 左键图标或全局快捷键；关闭后再次唤出 | 两种入口均可显示同一面板，关闭不结束后台，不自动扫描或清理 | mac原生 | ✅ | ui: 安装版从TextEdit快捷键打开、关闭后重开；组件真实NSStatusBarButton左键打开；auto: 原位按钮performClick/close两轮；panel-reveal |

## 测试

[本轮测试用例](menubar-presence.test-cases.md)。旧版启动自动显示已在修改前原生界面复现；新版覆盖隐藏启动、左右键菜单、两种唤起和关闭后重用。自动化仅复用Swift self-test和既有适配层测试；没有真实缓存删除或后台回收。

## 变更记录

| 日期 | 类型 | 说明 |
|---|---|---|
| 2026-10-23 | 用户要求 | 默认隐藏面板、菜单栏图标与快捷键按需唤起 |

## 证据边界

桌面执行器无法读取隐藏App的系统菜单栏extras。组件验证使用实际AppDelegate和真实NSStatusBarButton：先原位核对启动隐藏、窗口不恢复及按钮两轮打开/关闭，再将测试进程自己的按钮移到可抓取窗口，保留原target/action/event mask，通过computer-use真实左右键观察生产Dashboard和菜单，并选择退出。不是用渲染图或假回调替代。实际系统菜单栏位置/遮挡/命中没有直接覆盖；组件移位后的重复左键未稳定复现，不计作通过，重复唤出由原位按钮断言及安装版快捷键实测支持。没有清理真实缓存、关闭开发后台或修改调度。
