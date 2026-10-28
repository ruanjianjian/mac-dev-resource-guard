# 整段构建脚本接入测试用例

契约见 [design](../design.md)「构建：整段脚本模式」。

- TC-01 / AC-01：用项目入口加 `--cache-env MY_DD -- ./build.sh`，脚本把 `$MY_DD` 写进文件：两次运行拿到同一路径（等于 `xcode-path` 查询结果）；清单 `successful_builds` 从 0 到 2；真实 Xcode 小工程用脚本形式构建两次，第二次增量（日志无 `CompileC` 全量重编）。
- TC-02 / AC-02：同槽两个脚本并发，第二个在第一个结束后才开始（时间戳不重叠）；不同配置的两个槽同时开始。
- TC-03 / AC-03：脚本 `exit 7`、打印 stdout/stderr、被 SIGINT/SIGTERM：入口退出码与输出和直接运行一致；之后同槽能立即再次加锁。
- TC-05 / AC-05：缓存根配成 `/Volumes/<不存在的盘>/…`：报「缓存所在的盘未连接：<路径>」，退出码 2，未创建任何目录。
- TC-06 / AC-06：不带 `--cache-env` 的 1.3 写法：仍注入 `-derivedDataPath`，计数规则不变（查询类参数不计数）。
