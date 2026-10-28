# 询问档确认测试用例

契约见 [design](../design.md) C4～C6。模拟器与运行时用 `simctl` 桩；任务目录用真实临时 git 仓库（带本地 bare 远端）。

- TC-01 / AC-01：构造四类候选各一个：`pending.json` 出现 4 项，字段齐全（大小、理由、会失去什么）；`backend.py pending-preview` 返回 4 行且可勾选；写出 `notify-request.json`；看板菜单栏红点亮（界面）。
- TC-02 / AC-02：勾选 2 项 `pending-clean`：只执行这 2 项；执行前把其中 1 台模拟器改为已开机，该项返回「已跳过：状态已变」；另一项完成。
- TC-03 / AC-03：`pending-ignore` 某项：不再触发通知；改动其 fingerprint（模拟器启动时间变化）后重新变回 pending。
- TC-04 / AC-04：同一天连跑多轮只写一次 `daily` 请求；空间跌破低水位立即写 `critical`，回到低水位以上前不再重复。
- TC-05 / AC-05：有未推送提交的任务目录、开着且被 Xcode/`xcrun simctl` 进程附着的模拟器、被 pin 的对象：不进入 `pending.json`。
- TC-06 / AC-06：并发两次 `pending-clean` 同一 id、以及自动档正持锁时确认：同一项只执行一次，另一次返回 busy 或「已在处理」。
- TC-07 / AC-07：3 项中 1 项删除失败（桩返回非零）：成功的完成，失败的留在列表、`state=failed`、原因可见，可再次确认。
- TC-08 / AC-08：看板未运行时产生候选：后端调用 `open -g -j <App>`（桩记录）；看板启动后读到请求即发系统通知，点通知打开「待确认」（界面）。
- TC-09 / AC-09：确认删除一个 git worktree 任务目录：主仓库 `git worktree list` 不再有它、无 prunable 残留；它是指向外接盘的链接而外接盘未挂载时，该项显示「外接盘未连接」并不执行。
