# Mac Dev Resource Guard

Mac 开发资源诊断、已有 Xcode 缓存手动清理与托管后台回收，以及按需唤起的菜单栏「资源看板」。可作为完整 skill 交给 Codex / Claude 使用，也可以直接运行命令。

## 先用桌面看板

macOS 13+，安装 Xcode 或 Command Line Tools。App 内置清理引擎，首次安装即可扫描默认 Xcode DerivedData，无需安装 devlease 或修改构建脚本。已有 dev-storage / dev-memory 会优先复用，不覆盖。

```bash
git clone https://github.com/ruanjianjian/mac-dev-resource-guard.git
cd mac-dev-resource-guard
python3 widget/build.py
open "$HOME/Applications/资源看板.app"
```

启动默认隐藏面板，只在菜单栏保留小图标；左键点击图标或按全局快捷键呼出，右键图标打开刷新和退出菜单。面板显示内存压力、物理内存、压缩内存、swap、磁盘可用量、配置的空间目标、守护状态和固定构建槽数量。每15秒后台采样，可手动刷新、置顶；关窗后从菜单栏唤回。可打开系统活动监视器继续查看。默认全局唤起快捷键为 `⌃⌥⌘S`，避开保存用的 `⌘S`；注册冲突时依次尝试 `⌃⌥⌘R`、`⌃⌥⇧⌘S`，实际快捷键显示在面板底部和菜单栏。无需辅助功能权限。自动刷新只采样状态，不扫描缓存、不执行清理、不联网。手动清理与可选自动维护的操作见下方首次使用说明。它是菜单栏 App + 桌面小窗，不是 WidgetKit 通知中心扩展。

按当前 Mac 的架构本地编译，采用 ad-hoc 签名；没有 Developer ID 公证，未提供跨机器通用的预编译安装包。不修改 Gatekeeper 或系统安全设置。没有自动添加登录启动项。构建使用固定 Swift 模块缓存，不按每轮新建缓存。

## 安装 skill 和命令

首次使用可直接打开「磁盘缓存 → 已有缓存与大项占用」扫描，再确认清理默认 DerivedData 中至少七天未用的四类中间目录。Go 缓存、模拟器数据、Claude 会话和用户选定任务目录只统计大小，不直接删除；任务目录提供收尾核对命令。

「接入设置」展示命令来源、依赖缺口、实际调度加载状态与最近结果，提供安装命令工具和分别启停维护的按钮。安装和开启分别确认。App 内置后端支持暂停自动维护时的单次回收，旧后端不支持时明确提示升级，不临时恢复自动维护。

自动回收不保证释放指定容量。新用户可直接清理的是已有 Xcode 中间缓存；模拟器自动关机仍需兼容占用协议，MCP 仍需兼容 broker。未接入的资源保留，不把低 CPU 当闲置证明。

将完整仓库或 [Release 压缩包](https://github.com/ruanjianjian/mac-dev-resource-guard/releases/latest) 交给 agent：

> 使用 mac-dev-resource-guard，在我的 Mac 安装内存诊断与构建缓存复用，并接入我指定的构建脚本。先检查现有工具和调度，不覆盖、不打断开发；展示诊断后，再按我的授权启用周期维护。

也可手动运行：

```bash
python3 scripts/setup.py doctor
python3 scripts/setup.py install
```

Python 3.9+，无 pip 依赖。默认暂停自动回收、不安装调度。已有同名工具或状态时拒绝覆盖，需先核对迁移。Codex 与 Claude 共用同一份 skill、命令、状态和锁。

- [完整安装与可选调度](references/install.md)
- [内存回收机制及可选适配器](references/memory.md)
- [构建缓存如何复用](references/cache.md)
- [测试范围与未验证边界](references/validation.md)
- [桌面组件行为与验收](docs/widget.md)

## 共享什么

下载依赖缓存沿用 Go/npm/pnpm/pub/Gradle 的原生全局位置。同一工作树的 Xcode 构建按工具链、平台、配置复用固定 DerivedData，并用跨进程锁独占；不会每轮、每commit再造一份。不同工作树的 build、Pods、node_modules、.dart_tool 和可变 checkout 继续隔离。

直接 Xcode 构建接入示例：

```bash
dev-storage xcode --worktree "$PWD" --platform ios-simulator --configuration Debug -- \
  xcodebuild -workspace ios/App.xcworkspace -scheme App -configuration Debug -sdk iphonesimulator build
```

替换实际工程和scheme。已有脚本不会自动迁移，修改真正执行构建的入口并验证两次构建的实际路径后才算接入。

## 回收边界

内存压力需连续观察，候选需复核身份与闲置证据。Gradle支持指定版本的温和排空；模拟器需要兼容devlease占用协议，MCP需要兼容broker协议；本包不附带这些外部系统，缺失时保留相应资源。不会根据CPU=0或进程年龄强杀。

磁盘自动维护只处理专用缓存根中四类过期中间目录，保留源码、验收包、报告、登录态、设备和容器。磁盘目标只提醒，永远不阻止编译、依赖安装和测试。

## 验证

```bash
python3 scripts/verify.py
"$HOME/Applications/资源看板.app/Contents/MacOS/ResourceDashboard" --self-test
```

Python使用临时HOME与假设备／进程夹具；外部集成按条件跳过。桌面自测只读系统指标。真实项目构建命中率、第三方协议和其他Mac型号仍需在目标机器验证。
