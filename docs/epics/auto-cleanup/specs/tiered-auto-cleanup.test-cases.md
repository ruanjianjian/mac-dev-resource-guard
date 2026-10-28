# 自动档清理测试用例

契约见 [design](../design.md) C1～C3。全部用隔离 HOME（`tempfile` + 假 `df`/空间阈值）；真实盘实测用 T9 上的隔离目录。

- TC-01 / AC-01：两个任务根各放 3 个任务目录（闲置 5/4/1 天），配置 external 指向 T9 隔离目录，`auto_enabled=true`，把提醒线/目标线设成「挪完第一个就回到目标线」。跑一轮：只挪闲置最久的那个，原处变成指向外接盘的链接；在链接下 `git status`、`git log -1` 与搬前一致；再跑一轮不重复搬。
- TC-02 / AC-02：external 未配置、或指向 `/Volumes/<不存在的盘>`：闲置任务里的 `build/`、`node_modules/`（git 忽略）被删，被 git 跟踪的同名目录、源码、`.env` 保留；已是链接的任务目录不动；回执 `external_missing` 非空，看板行提示「外接开发盘未连接」。
- TC-03 / AC-03：分别构造进程 cwd 在目录内（子进程 `sleep`）、打开文件、2 天内改写、pin 的任务目录：都不搬不删，`kept` 写明原因；另构造「有未提交改动但闲置 5 天」的目录：照常被搬迁。
- TC-04 / AC-04：在复制阶段注入一次文件新增（patch 复制钩子）：本次放弃，外接盘无残留，原目录完好；下一轮成功。
- TC-05 / AC-05：`simctl list devices --json` 打桩出 1 台 `isAvailable=false` 和 2 台正常设备：只对不可用那台调用 `simctl delete <udid>`；工具缓存只调用工具自带命令（`xcrun simctl delete unavailable` 之外不直接删设备目录），桩记录调用参数。
- TC-06 / AC-06：`auto_enabled=false`（含 C1 文件不存在、从 1.3 升级）或空间高于提醒线：零动作，回执 `status` 分别为 `disabled` / `healthy`；1.3 的缓存清理仍按原样执行。
- TC-07 / AC-07：先占住 `periodic-storage` 锁再跑一轮：回执 `busy`，零动作。
- TC-08 / AC-08：执行若干动作后 `backend.py auto-preview` 返回「自动处理记录」行：动作类型、路径、腾出大小、保留项与原因。
- TC-09 / AC-09：分别在 `copying`、`swapping` 阶段留下 `auto-tier-move.json` 模拟中断：下一轮先收拾（删不完整副本 / 认定完成），原目录与链接至少有一个可用。
- TC-10 / AC-10：改 `auto_idle_days`、`task_roots`、间隔后下一轮按新值；写错（相对路径、天数 0、external 指向卷根、未知字段）时本轮不清理，回执 `config_error` 指出字段；1.3 的 `config.json` 不被改动，`dev-storage status` 照常。
