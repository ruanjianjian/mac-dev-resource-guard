# Mac Dev Resource Guard · 资源看板 1.3

**完整安装后自动维护开发缓存，项目接入后复用 Xcode 构建缓存。** 面板默认隐藏，需要时点击菜单栏图标或按 `⌃⌥⌘S` 打开。

适合同时运行 Xcode、模拟器、多项目和多个 Agent 的 Mac 开发同学。内置 dev-storage 与 dev-memory，无需另外安装；已有同名私有工具和保护记录保留。

[看宣传短片与安装说明](https://ruanjianjian.github.io/elys-skill-foundry/?v=resource-guard-1.3#mac-dev-resource-guard) · [详细使用说明](https://hcnqwxuq730f.feishu.cn/page/OrtGm3ZDDdm1cEaDAnVctlBBn7g)

## 安装

环境：macOS 13+、Python 3.9+、Xcode 或 Command Line Tools；iOS 构建需要完整 Xcode。无需 pip 依赖。

```bash
git clone https://github.com/ruanjianjian/mac-dev-resource-guard.git
cd mac-dev-resource-guard
python3 scripts/install.py
```

看到以下输出，才算完整安装成功：

```text
安装完成：安全自动维护已启用，面板默认隐藏。点击菜单栏图标查看结果。
```

完整安装会安装 App、维护引擎与后台调度，并开启安全自动维护。若提示“部分完成”或报错，按具体错误处理，不要当作已开启。仅运行 `widget/build.py` 只构建 App，不能替代完整安装。

App 位于 `~/Applications/资源看板.app`，启动后不打开面板。快捷键冲突时尝试备用组合，实际组合显示在窗口底部。自动维护可在“接入设置”暂停。

## 安装后会自动做什么

| 能力 | 实际范围 |
|---|---|
| 磁盘维护 | 安装后一次，此后每 12 小时检查固定构建槽与默认 Xcode DerivedData；仅清理符合保留期和安全检查的四类中间缓存，默认 DerivedData 至少七天。DerivedData 换成指向外接盘的链接时按真实目录清理。 |
| 内存维护 | 每分钟检查，有归属与闲置证明才收尾支持的托管后台；不是定时强制释放内存。 |
| 占用保护 | 删除前复核进程、打开文件、槽锁、pin、lease、最近使用和路径身份；在用、受保护、状态不明的内容保留。 |
| 原有安装 | 保留已有命令、合法缓存根和保护状态，复用可验证的旧守护；未知自定义调度会报告未完成，不启动第二套。 |

四类中间目录为 `Intermediates.noindex`、`Index.noindex`、`ModuleCache.noindex`、`SDKStatCaches.noindex`。保留源码、Products、任务目录、测试证据、模拟器数据和登录态；不强杀用户应用、不删除 swap。模拟器与 MCP 缺少兼容占用或排空协议时保留。

Go 编译缓存、模拟器、Claude 会话和任务目录可查看占用，但不自动删除。没有安全候选时可清理量就是 0；目录计量不等于 APFS 全盘净释放空间，不承诺固定回收容量。磁盘空间目标只提醒，不阻止构建。

## 接入 Xcode 构建缓存

安装 App 不会改写已有项目脚本。每个项目接入一次，之后通过生成的入口构建，才会使用固定缓存槽。可在面板“接入设置 → 接入项目构建缓存”选择项目，也可以全程用终端：

```bash
cd "/你的项目目录"
"$HOME/.local/bin/resource-guard-service" integrate --project "$PWD"
```

之后执行，例如：

```bash
./.dev-resource-guard/xcode-build \
  --platform ios-simulator --configuration Debug -- \
  xcodebuild -workspace ios/App.xcworkspace \
  -scheme App -configuration Debug -sdk iphonesimulator build
```

将工程路径和 scheme 换成项目实际值；平台可选 `ios-simulator`、`ios-device`、`macos`，入口配置与原构建参数保持一致，不另传 `-derivedDataPath`。原脚本和 Agent 必须实际调用这个入口才算接入。生成的 `.dev-resource-guard/` 自带忽略规则，不会出现在 `git status` 里，也不改项目的 `.gitignore`。

同一工作树、工具链、平台和配置使用同一个槽，并通过跨进程锁串行写入；不同工作树的可变构建输出保持隔离。Go、npm、pnpm、pub、Gradle 用户下载缓存沿用工具默认全局位置，不给每个任务复制一份。Flutter 自身构建继续使用每工作树输出，不强行重定向。

## 更新

在工具仓库目录执行：

```bash
git pull --ff-only
python3 scripts/install.py
```

安装器只更新本工具 App；构建和维护进程保留原运行时直至结束。本地按当前架构编译并 ad-hoc 签名，无 Developer ID 公证，不修改 Gatekeeper。

## 可直接转发的介绍

> 给 Mac 开发同学推荐「资源看板 1.3」：完整安装后自动维护过期 Xcode 中间缓存，项目接入后复用构建缓存，减少同一任务重复建缓存。面板默认隐藏，菜单栏或 ⌃⌥⌘S 随时呼出；在用、受保护或无法确认安全的资源会保留，不自动删除任务和模拟器。无需另装 dev-storage、dev-memory。已有构建脚本需要改用生成的入口，才会使用固定缓存槽。
>
> 短片与安装：https://ruanjianjian.github.io/elys-skill-foundry/?v=resource-guard-1.3#mac-dev-resource-guard

## 验证与边界

1.3 已在真实 Mac 完整安装，自动清理 5 项真实缓存，目录计量约 4.86 GiB；真实 Xcode 连续构建验证了同槽复用，项目查询不会计作成功构建。此结果不是每台机器的容量承诺。

1.3.1 修复：DerivedData 是链接时整轮自动清理失败；某一类失败后看板仍只显示「已加载」；项目接入后仓库多出未跟踪文件。自动化增至 253 项（238 通过、15 项外部集成条件跳过）。

自动化共 245 项：230 通过、15 项外部集成条件跳过；CI 通过。最终原生交互复验按用户要求停止，后台实测不替代原生验收，未宣称人工验收完成。[实现与验证 PR #4](https://github.com/ruanjianjian/mac-dev-resource-guard/pull/4)

- [维护与构建规格](docs/epics/resource-dashboard/specs/automatic-maintenance.md)
- [安装与兼容入口说明](references/install.md)
- [内存保护](references/memory.md)
- [缓存复用](references/cache.md)

隔离测试：`python3 scripts/verify.py`。真实独立 launchd 验证：`python3 scripts/verify_automatic.py --home <全新测试目录>`；它只清理自建测试缓存并保留 pin/Products，结束后卸载测试标签。
