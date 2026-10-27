# 安装与持续维护测试

- TC-AUTO-01 / AC-AUTO-01：从完整安装命令安装，检查 launchctl 与后台设置状态；旧内存守护只有一份，App 冷启动隐藏。已公开 1.2 调度空闲迁移，忙碌保留并恢复派发。
- TC-AUTO-02 / AC-AUTO-02：真实唯一测试标签 RunAtLoad 删除过期测试中间文件，Products/pin 留存；本机安装后读取真实自动回执与候选复扫。暂停期间及未知占用零删除。
- TC-AUTO-03 / AC-AUTO-03：通过后台项目接入生成入口（原生选择另列 TC-AUTO-04），使用其原构建参数真实运行 Xcode 两次；manifest 同 key/path 且成功计数为2；-list 不增加。后台状态显示同槽复用已验证。
- TC-AUTO-04 / AC-AUTO-04：原生安装按钮、项目选择与结果呈现完整复验；CH-01 用户要求停止弹窗，本轮明确未完成。
- TC-AUTO-05 / AC-AUTO-05：隔离 HOME 中把 `Library/Developer/Xcode/DerivedData` 换成指向专用目录的链接，放置过期中间缓存与 Products：`common.preview/apply` 与 `service.tick_storage` 均删除过期缓存、保留 Products。链接指向 HOME、`/`、HOME 的上级目录或卷挂载点时 `common.manager` 拒绝；指向不存在的位置时抛出 `CacheUnavailable`，tick 把该类记为 skipped，托管槽一类照常执行。本机实测：真实 HOME（DerivedData → /Volumes/T9）跑一轮 `tick-storage`，回执 status 不为 error。
- TC-AUTO-06 / AC-AUTO-06：`common.manager` 抛错时 tick 不中断，回执 `automatic-storage.json` 写入 status=error、该类 error 文本、其他类结果；配置非法时两类都记 error 并写回执。`setup_status.status` 对 error 回执显示「失败」与原因，对 skipped 显示「已跳过」与原因；无回执而 `storage-scheduler.err` 有内容时显示「失败」与最后一行。
- TC-AUTO-07 / AC-AUTO-07：临时 git 仓库根目录与子目录分别 `service.integrate`，`git status --porcelain` 为空、`.gitignore` 不存在或不变；删除新加的忽略文件模拟 1.3 已接入项目，再次 integrate 后恢复干净；非 git 目录照常生成入口。
