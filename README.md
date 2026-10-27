# Mac Dev Resource Guard

**1.3 完整安装体验**：下方 `scripts/install.py` 一次完成 App、完整引擎与自动维护。安装后立即开始、此后每 12 小时检查并清理安全的过期中间缓存（固定槽 + 至少七天未用的默认 Xcode DerivedData）；内存每分钟观察，有闲置证明才收尾。无需每次手动触发；可在接入设置暂停。源码、Products、pin、设备与任务目录保留。

已有私有命令保留；看板使用内置完整协议，共用现有合法缓存根与保护状态。已有内存守护会复用，未知调度会明确报部分完成，不能假称开启成功。仅使用 `widget/build.py` 是构建 App，需再点「完成安装并开启维护」。

构建缓存：接入设置选择项目，生成 `.dev-resource-guard/xcode-build`。将原构建命令经该入口执行（`--platform ios-simulator --configuration Debug -- xcodebuild <原参数> build`），同工作树同工具链同配置复用槽；页面以真实成功构建次数验证复用。安装不会偷偷改写项目原有脚本。Go/npm/pnpm/pub/Gradle 沿用工具全局共享缓存，不复制到每个任务。


macOS 13+，Python 3.9+，已安装 Xcode 或 Command Line Tools，无 pip 依赖。

```bash
git clone https://github.com/ruanjianjian/mac-dev-resource-guard.git
cd mac-dev-resource-guard
python3 scripts/install.py
```

更新：`git pull` 后再次运行 `python3 scripts/install.py`。构建使用固定 Swift 模块缓存；App 本地按当前架构编译并 ad-hoc 签名，无 Developer ID 公证，不修改 Gatekeeper。

安装后面板默认隐藏，菜单栏图标或 `⌃⌥⌘S` 呼出；冲突时尝试 `⌃⌥⌘R`、`⌃⌥⇧⌘S`，实际组合显示在窗口底部。自动维护在后台运行，面板每15秒只采样状态。

## 维护范围

- 自动磁盘：固定构建槽与默认 DerivedData 的 Intermediates.noindex、Index.noindex、ModuleCache.noindex、SDKStatCaches.noindex。删除前复核槽锁、pin、lease、进程、打开文件、保留期与路径身份；空间目标只提醒，不阻止构建。
- 自动内存：有归属与闲置证明的托管日志、支持的 Gradle 后台；模拟器和 MCP 仍需兼容的外部占用/排空协议。不强杀现有应用，不用 purge，不删 swap。
- Go 编译缓存、模拟器数据、Claude 会话与选定任务目录显示占用；不会直接删除。任务只提供 git 核对命令。
- 接入设置展示实际调度（包括旧守护）、暂停状态、最近清理回执和项目复用证据。目录统计不等于 APFS 实际释放量，不承诺固定释放容量。
- 自动维护可暂停。关闭不终止已经执行的文件操作；下一候选复核暂停。对于无法验证的自定义调度，保留原配置并显示未完成，不重复安装。

## 构建接入

原生面板「接入设置 → 接入项目构建缓存」选择项目根目录，新增一个独立入口，不覆盖原构建脚本。使用原工程/scheme参数：

```bash
./.dev-resource-guard/xcode-build --platform ios-simulator --configuration Debug -- \
  xcodebuild -workspace ios/App.xcworkspace -scheme App -configuration Debug -sdk iphonesimulator build
```

也可使用完整安装提供的 `resource-guard-storage xcode --worktree "$PWD" ...`。新用户没有 dev-storage/dev-memory 时会创建别名；已有同名工具不覆盖。其他 agent 或原脚本必须实际调用入口才算接入，不能将安装 App 当作所有项目已经接入。

## 验证与文档

`python3 scripts/verify.py` 运行隔离测试。`scripts/verify_automatic.py --home <全新测试目录>` 使用唯一 launchd 标签实际清理测试缓存并保留 pin/Products，完成后卸载测试标签。

- [安装说明](references/install.md)
- [维护与构建规格](docs/epics/resource-dashboard/specs/automatic-maintenance.md)
- [内存保护](references/memory.md)
- [缓存复用](references/cache.md)

同工作树稳定槽复用已经由两个真实 Xcode build 验证；不同工作树可变目录保持隔离。第三方协议不具备条件时明确跳过，不能当作实际验收。
