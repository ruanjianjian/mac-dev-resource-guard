# 内存诊断与安全回收

```bash
dev-memory status --json
dev-memory inspect --json
dev-memory clean --json
dev-memory history
dev-memory pause
dev-memory resume
```

先看系统压力、压缩内存和 swap in/out 的变化，再按进程归属查原因。RSS/MEM 只能估计进程记账，不可相加当可释放 RAM；已有 swap 不会因为某个进程退出立即归零。

`status` / `inspect` 只观察；`clean` 请求相同安全策略，暂停时也不执行动作，不是强制回收。清理与 pause 共用全局锁。启动身份采用 kern.bootsessionuuid；候选 PID 同时核对出生时间，防止 PID 复用。

- Gradle：连续两次相同闲置证据至少间隔 30 秒，版本、registry、daemon UID、PID 身份与入站消费者数一致，才对单 daemon 发送官方 StopWhenIdle。最终检查后新来的构建由协议正常等待结束；不执行版本级 gradle --stop。未知版本、忙碌后台保留。
- 模拟器：装了 devlease 时，必须证明 agent 管理、原 owner/run 已结束、无测试进程；用完整租约签名 CAS 复核后正常关机，核实成功才释放。没装 devlease 时用自带检查：按 launchd_sim 进程树算每台内存，按命令行找连着的开发工具（xcodebuild、simctl、baguette、脚本等）；没被连着满两小时、两次观测一致、且内存紧张（压力 2 或 4）时，关机前再查一次仍没人连才 `simctl shutdown`；更近没被连着的只在内存页一键关。任何新占用或未知结果保留。不会删除设备、App 和登录态。
- 告警：警告级压力持续 2 分钟提醒，很紧立刻提醒；采样间隔 3 到 15 分钟只重置回收计数、不重置告警计时；送达（资源看板写回执，或 AppleScript 成功）才记为已通知，否则下一轮重试；仍紧张每 30 分钟提醒，变很紧再提醒。
- MCP：显式配置且能力标记可信后，broker 原子阻止新消费者、核对消费者/请求/任务/工具调用全为零，仅关闭它自己 app-server 的 stdin，等待退出。旧 broker 无 RPC 探测回收、无强杀；本包不包含项目插件补丁。
- tail：只接管由 `dev-memory run --owner-pid <本会话PID> --kind tail -- /usr/bin/tail ...` 创建的专有日志进程；不接管其他进程。

持久化 intent 后才请求退出；accepted 不是 stopped。未知结果写 pending，只读核对，不重发。超时不升级 SIGKILL。回收前重新核对身份与活动；桌面 App、构建/测试/热重载、设备占用、共享服务及活跃会话进程树受保护。

`~/.local/state/dev-memory/` 保存观测、动作、pending 和冷却；不清这些文件来绕过保护。Gradle helper 在该状态目录按版本固定复用并加编译锁；不把整个 Gradle 全局缓存复制给每个任务。

通报仅在持续压力升级、失败、需要人工处理或有可核实动作时发出，未变化不反复提醒。报告具体对象、理由、退出确认、RSS估计和系统指标前后值；不要把整个机器的指标变化全算作工具功劳。
