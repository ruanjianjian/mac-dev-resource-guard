# 安装、启用与撤回

## 1.3 完整安装与升级（推荐）

推荐 `python3 scripts/install.py`。安装动作包含开启安全自动维护；安装前告知范围：固定槽和至少七天未用的标准 Xcode 中间缓存，保留源码、Products、pin、lease、设备和任务。已有守护核实后复用，不重复启动。未知调度报部分完成；已知旧公开调度仅在空闲时迁移，忙碌时保留并允许重试。

更新后同命令重跑，原私有 dev-storage/dev-memory 保留，新引擎使用独立命令 resource-guard-storage/resource-guard-memory。运行中的旧版本进程继续自然结束，下次调度使用原子更新的入口。App 默认隐藏，不弹出面板。

1.3.1 起，默认 Xcode 缓存目录是指向专用目录（例如外接盘）的链接时照常清理；链接指向主目录、磁盘根目录或整块盘时拒绝这一类，指向的位置不存在时本轮跳过并注明。

下面的 setup.py 分步安装为兼容接口，新用户无需逐项执行。

## 分档自动清理（1.4）

升级到 1.4 后重跑 `python3 scripts/install.py`：本工具的磁盘定时任务从 12 小时改为每 5 分钟唤醒（空闲时才重新加载，忙时保留并提示稍后重试）。自动档默认关闭；询问档默认打开，只通知、不自动执行。设置写在 `~/.config/dev-resource-guard/auto-cleanup.json`，至少填 `task_roots` 才会处理任务目录；字段与默认值见 [设计 C1](../docs/epics/auto-cleanup/design.md)。首次发通知时 macOS 会请求通知权限，由你决定是否允许；不允许时只亮菜单栏红点。

## App 内置入口（1.2）

仅构建 App 即包含扫描与手动清理引擎。默认 DerivedData 不要求构建包装接入；Go、模拟器、Claude 会话与用户选定任务目录只统计。已有命令优先复用，旧版本不支持单次回收时明确提示升级，不临时 resume 再 pause。

「接入设置」检查 launchctl 实际加载状态；安装命令与启停定时维护分别确认。命令安装冲突会说明原工具被保留，不自动迁移或覆盖。新命令提供 `dev-memory clean-once --id <预览资源ID> --json`，保持自动暂停标记，仍执行闲置、身份、占用、冷却和 pending 检查。`clean`、`tick` 仍受暂停标记约束。已有 Xcode 缓存仅供手动清理，不加入周期删除白名单。

将完整 zip 解压后，把文件夹交给 Codex / Claude，并说：

> 使用这个 mac-dev-resource-guard skill，在我的 Mac 安装内存诊断与构建缓存复用。先检查现有工具和调度；不要覆盖、不打断开发。展示诊断和可回收范围后，再按我的授权开启定期维护。

在解压目录执行（Python 3.9+，macOS）：

```bash
python3 scripts/setup.py doctor
python3 scripts/setup.py install
```

安装至 `~/.codex/skills/mac-dev-resource-guard`，Claude 的同名 skill 符号链接指向它；命令包装器在 `~/.local/bin/`，两端共用状态和锁。命令不在 PATH 时使用绝对路径，或自行将 `~/.local/bin` 加入 PATH。安装器不会改 shell、AGENTS.md、hooks、插件、设备或账号。不要使用 sudo。`--home` 仅用于隔离安装测试。

已有 dev-memory / dev-storage 时安装器拒绝覆盖；优先复用原实现。若确需迁移，先检查在途锁、pending 请求及兼容性，维护窗口中迁移，保留原状态和回滚入口，不并行跑两套。单纯分享本包不需要迁移来源机器的部署。

## 安装后的能力

| 能力 | 必要条件 / 缺少时行为 |
|---|---|
| 内存压力、压缩、swap、进程归属诊断 | macOS 系统命令与 libproc；失败保留 |
| Gradle 单 daemon 温和回收 | 当前支持 8.14 / 8.11.1，相应 JDK 和发行版；其他版本保留 |
| iOS / Android agent 模拟器回收 | 本机已有兼容 devlease 的 memory-inspect / memory-reap CAS 协议及正确登记；本包不安装该工具、hooks 或账号池，缺少时所有设备保留 |
| MCP 正常排空退出 | 兼容 codex-memory-drain-v1 的 broker，设置 DEV_MEMORY_BROKER_SOURCE 为其真实 app-server-broker.mjs；未配置、旧协议、未知状态保留，不给活跃插件打补丁 |
| Xcode 固定编译缓存 | 已安装 Xcode，调用构建包装入口；不依赖模拟器回收协议 |
| Flutter / Android / Go / JS 依赖共享 | 沿用工具原生全局下载缓存；不迁移已有项目输出 |
| 托管日志 tail | 显式 run 入口，owner 身份及保护表完整；不收养已存在的陌生 tail |

自动清理默认暂停，调度默认不安装。先运行：

```bash
~/.local/bin/dev-memory status --json
~/.local/bin/dev-storage check
~/.local/bin/dev-storage plan
```

`status` 会写自己的观测日志，但不关闭进程。没有 devlease 并不妨碍诊断或独立的 Gradle 协议检查；设备和不能证明安全的 tail 始终保留。

## 可选定期维护

先检查 `~/Library/LaunchAgents` 和 Codex/Claude 已有的计划任务，复用同一入口，不能重复安装。安装器能识别常见同名 LaunchAgent，但不能发现所有云端或应用内调度，agent 必须补查。

用户授权自动内存回收后：

```bash
python3 scripts/setup.py enable-scheduler memory
~/.local/bin/dev-memory resume
```

每 60 秒观察；正常压力的例行收尾最多每天一次，持续压力经复核可触发安全回收，统一十分钟冷却和每轮最多三项。`pause` 暂停动作但保留监测。

用户授权四类过期中间缓存清理后，选择应用内每 12 小时自动化 **或** 以下 LaunchAgent，二选一：

```bash
python3 scripts/setup.py enable-scheduler storage
```

启动程序使用安装时 Python 的绝对路径；升级／卸载 Python 后需检查调度。Mac 关机或退出登录期间不保证执行；不声称定时任务已实际触发，需看后续日志。内存详细事件自动轮转，调度 stderr 在 `~/.local/state/dev-resource-guard/`，持续报错应修复或停调度。

## 配置与停止

`~/.config/dev-resource-guard/config.json`：安装时按文件系统总容量生成建议值：目标20%、提醒15%、低水位10%，都只提醒。可调整 `target_free_gib`、`warning_free_gib`、`critical_free_gib`、`intermediate_budget_gib`；默认缓存根 `~/Library/Caches/dev-resource-guard`，状态根 `~/.local/state/dev-storage`。不要让状态放进缓存根，也不要把整个 HOME 设为缓存根。配置在安装后新命令启动时读取；在途构建期间不要改缓存／状态根，换根会失去旧锁与pin关联。500/600GiB不是适用于所有磁盘的默认值。

```bash
~/.local/bin/dev-memory pause
python3 scripts/setup.py disable-scheduler memory
python3 scripts/setup.py disable-scheduler storage
```

停调度不会终止正在执行的维护或构建；等待其自然结束再卸载。卸载时仅移除本包创建且仍指向它的命令、Claude 链接和 skill 文件夹；保留缓存、状态、pin 与 pending，另行审阅后处理。不删除进程或用户数据来“卸载”。

## 已有版本升级

在接入设置点「升级本工具」，或在新版本源码执行 `python3 scripts/setup.py upgrade`。只接受本工具标准命令包装器和已知发布版本文件；不同实现或本地修改保持原样。先关闭本工具的调度，并等在途维护、托管后台与待核实请求自然收尾。升级先构造完整副本、校验后切换，失败恢复旧目录，备份路径写入结果；配置、pin、lease 和自动暂停状态不变。升级不等于开启自动维护。
