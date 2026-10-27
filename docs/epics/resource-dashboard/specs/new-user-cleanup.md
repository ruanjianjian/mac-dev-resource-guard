# 新用户可用的资源清理

现象：只安装看板时清理依赖缺失、既有缓存未覆盖；暂停自动回收还会挡住手动操作。目标是新用户有可操作入口，不能安全处理的项目明确解释。

## 行为与范围

- App 内置 runtime，不要求 Codex、Claude、devlease；已有命令优先复用，不覆盖状态。安装与开启自动维护是两个独立确认动作。
- 磁盘默认扫描用户标准 Xcode DerivedData，仅手动清理至少七天未使用的四类中间目录。自动磁盘维护仍仅托管槽，不自动删除其他目录。
- Go 默认编译缓存、iOS 模拟器、Claude 会话及用户选择的任务目录显示大小与保留原因。大小不可读不显示为零；任务只提供 git 核对命令。
- 手动清理仅针对有效预览中的项目，执行前复核，超过空间目标也可按确认执行。自动内存暂停不阻止新版 clean-once，但仍保留活跃、未知、冷却和 pending 对象。旧后端不支持时说明升级需求。
- 接入设置显示命令来源、设备与 MCP 依赖、launchctl 实际加载、最近结果；可安装命令和启停维护。窗口仍默认隐藏。

## 验收标准

| ID | 级别 | 落点 | 验收条件 | 状态 | 证据 |
|---|---|---|---|---|---|
| AC-NU-01 | 核 | macOS App 磁盘页 | 无外部工具的隔离 HOME 能预览、确认并实际删除过期中间文件，复扫消失，Products/保护对象保留 | ✅ | ui: NU-cache-r2; auto: test_common_cleanup + test_dashboard |
| AC-NU-02 | 核 | macOS App 开发后台 | 自动暂停时新版单次回收可用，旧版明确提示；仅预览对象可被复核回收，不恢复自动状态 | ✅ | ui: NU-memory-r1 (real tail exited, pause retained); auto: owned-only and legacy regression |
| AC-NU-03 | 核 | macOS App 接入设置 | 显示真实依赖及调度状态，确认安装/启停入口可用，冲突保留原工具 | ✅ | ui: NU-setup-r2; auto: real unique-label launchd lifecycle + upgrade rollback |
| AC-NU-04 | 核 | macOS App 磁盘页 | 大项占用和未知大小明确，任务目录只给核对命令；启动隐藏、快捷键与菜单栏可用 | ✅ | ui: NU-inventory-r1 (clipboard, shortcut, menu icon) |

## 代价与限制

无新增第三方依赖或联网。扫描由点击触发，读取文件元数据和进程占用；大项 du 每项最多等待八秒。默认路径之外的自定义 Go 缓存不自动发现。未接入占用协议不自动关闭模拟器，未配置 broker 不回收 MCP；不保证释放指定容量。人工验收另记，不能用自动化冒充。

## Validation evidence

Real filesystem and native UI verification used disposable HOME directories. No real user caches or tasks were deleted. The original implementation fails the manual-paused regression (red-baseline.log); current source and packaged installation tests pass. Native latest-tree journeys are selected by the delivery plan; human acceptance is not claimed.
