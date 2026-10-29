# 给 agent 的说明

这个仓库是「资源看板」（Mac Dev Resource Guard）：Mac 开发者的磁盘空间管家。它把各任务的构建缓存共享起来、用好外接盘，后台只做能恢复的清理，删东西前先问用户。

先分清你来做什么。

## 用户让你安装或使用资源看板

不用克隆仓库，也不用读代码，照 [SKILL.md](SKILL.md) 做：

- **新装只问用户一句**：默认打开共享构建缓存、内置盘至少留多少（约磁盘的 20%）。用户说可以，就跑 SKILL.md 里那一行安装命令，装完就已开好。
- **升级什么都不用问**，直接跑安装命令。1.7.7 起资源看板会自己更新；用户说「更新资源看板」时运行 `~/.local/bin/resource-guard-service update`。
- 装好后按 SKILL.md 第 2 节出计划。会丢东西的每一项都先跟用户说清楚、等用户点头。
- 不要直接 `rm -rf`，不要改 macOS 的安全与隐私设置。系统问资源看板能不能访问外接盘时，请用户自己点「允许」。

## 你在改这个仓库

### 代码在哪

| 位置 | 是什么 |
|---|---|
| `install.sh`、`scripts/install.py` | 一行安装：下载源码、在本机编译、装好 App、后台任务和 skill |
| `scripts/service.py` | 后台命令 `resource-guard-service`：定时整理、出计划、自动更新 |
| `runtime/storage/` | 磁盘：共享缓存、分档清理、外接盘、计划；`updater.py` 是自动更新 |
| `runtime/memory/` | 内存（dev-memory）：只回收有闲置证据的开发后台 |
| `widget/` | 菜单栏 App（SwiftUI）。`main.swift` 入口，`Cover.swift` 封面和各页，`Logo.swift` 小留，`backend.py` 给 App 供数据，`build.py` 编译，`local_signing.py` 本机固定签名 |
| `tests/`、`runtime/*/tests/` | 自动化测试 |
| `SKILL.md`、`agents/openai.yaml` | 装到用户机器上的 skill（Claude Code、Codex 都用） |
| `docs/design.md` | 产品设计：原则、视觉、文案、logo |
| `docs/epics/` | 需求规格和验收标准。行为以这里为准 |
| `references/` | 安装、签名、自动更新与发版、内存、缓存的细节 |

### 怎么测

```bash
python3 scripts/verify.py                                          # 全部自动化测试，HOME 指向临时目录
python3 widget/build.py --output "$TMPDIR/rg/ResourceDashboard.app"  # 编译 App
"$TMPDIR/rg/ResourceDashboard.app/Contents/MacOS/ResourceDashboard" --self-test   # 只读自检
```

CI 在 macOS 上跑同样几步，push 和 pull_request 各跑一次。

改了界面就出图看效果：`ResourceDashboard --test-home <目录> --render-tab <页签> <输出.png>`。目录里要有 `.resource-dashboard-fixture` 标记文件，不弹窗口，也不读真实的主目录。

### 规矩

1. **测试只碰临时目录。** 不读写真实的 `~`，不退出、不重启用户正在用的资源看板，不弹面板。以前有个测试按 bundle id 找进程，把真实 App 关掉了。
2. **安全边界和 [SKILL.md](SKILL.md)「不变的边界」一致。** 正在用的、被保护的、有未提交或未推送改动的、判断不了的，一律保留。后台自动做的事从不删任务。
3. **签名只走 `widget/local_signing.py`。** 不要改回临时签名，也不要改系统的信任设置。否则用户每次升级都要重新授权外接盘这些权限。
4. **给人看的文字用简体中文大白话**，包括界面、通知、README、SKILL.md。用词照 [docs/design.md](docs/design.md)「文案」。
5. **改了行为，同步改 `docs/epics/` 里对应的规格。**
6. **改动走 PR。** CI 两项都通过后 squash 合并。
7. **合进 main 不等于发版。** 只有仓库主人说「发版」时才发：改 `widget/build.py` 里的版本号和 README 标题 → 合入 → `gh release create vX.Y.Z`。Release 说明的第一行会原样出现在所有人收到的更新通知里，要写成一句给人看的话。细节见 [references/install.md](references/install.md)「自动更新与发版」。
