# 安全自动维护 1.6.1

## 原始输入

见 [原始输入](safe-cache-maintenance.raw-input.md)。用户要求把本机安全补丁推广到所有安装用户，保留正在运行的任务，后台验证，更新公开安装说明并提交合并。原 1.4 自动档的任务搬迁/裁剪、不可用模拟器删除及整份工具缓存清理规则由本规格替代。

## 意图与边界

自动清理仅指固定构建槽和默认 Xcode DerivedData 中至少 7 天未用的四类中间缓存：Intermediates.noindex、Index.noindex、ModuleCache.noindex、SDKStatCaches.noindex。空间目标不成为构建门禁，也不放宽保留期。任务目录、模拟器、Products、证据、登录态与整份工具缓存不进入自动删除路径。

新安装默认启用该安全维护。升级兼容缺少 auto_scope、旧 standard 和本机先行 managed-caches 配置，并统一安全范围，保留暂停选择。旧迁移记录不自动恢复，也不得被新缓存迁移覆盖。用户单独同意的外接工具缓存迁移仍可使用，外接盘缺失/拒绝访问不触发任务裁剪。

共享缓存仍需开启；不把不同工作树的可变产物共写。现有构建继续运行，新开的终端/IDE继承全局 Xcode 设置。界面默认隐藏。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 新用户、旧版 standard 或本机先行配置，空间低于目标 | 载入配置并执行自动档 | 统一安全范围，不自动删/搬任务、删模拟器或清整份工具缓存；旧恢复记录保留 | CLI后端 | ✅ | auto: test_managed_scope 的新旧配置、默认设备保护、旧 marker 调度回归；红: d14eed2 上新增安全测试失败 |
| AC-02 | [核] | 缓存过期或在用、被保护、含受保护产物 | 周期维护 | 两类缓存均至少保留 7 天，复用进程/文件/锁/pin/lease与身份复核；安全候选才执行 | CLI后端 | ✅ | auto: test_scheduled_cache_maintenance_preserves_seven_days_in_both_families及既有storage保护测试；真实Mac：2处/180 KiB，复扫105项保留；红: 基线无统一7天约束 |
| AC-03 | [核] | 自动清理关闭、扫描中关闭，或设置错误 | 定时维护/重新启用 | 不删除；失败写新回执，不能显示旧成功；重新启用恢复检查 | CLI后端 | ✅ | auto: test_pause_blocks_real_expired_cache_then_enable_resumes、test_disable_during_scan_prevents_delete、test_invalid_config_replaces_stale_success_receipt_without_deletion；红: 基线无auto_enabled删除门 |
| AC-04 | [核] | 已暂停/旧维护在途/旧配置错误 | 更新安装 | 保留暂停；忙时保持旧App，等待下次重试，不杀维护；错误配置在替换前拒绝 | 安装器 | ✅ | auto: test_activate_keeps_existing_pause_without_resuming_memory、test_installer_does_not_replace_app_while_maintenance_runs、test_installer_rejects_invalid_settings_before_any_subprocess；红: 基线activate移除storage-paused且安装不取维护锁 |
| AC-05 | [核] | 新版构建与后台操作入口 | 安装并开启缓存/安全清理 | 安装包自检通过，不弹面板；Xcode设置可被新进程读取，同工作树可重用缓存，外接盘关闭 | CLI、App启动 | ✅ | 真实验证与安装记录见公开验证报告；不以夹具容量替代真实结果 |
| AC-06 | [边] | 原生操作面板 | 查看新文案与计划 | 只描述安全范围，不将整份任务容量计入自动收益 | CLI、mac原生文案 | ⚠️CH-01 | auto: 计划/后端行回归和Swift自检；前台文案点击验收未做，用户要求全程后台不弹面板 |

## 流程

```mermaid
flowchart TD
  A["安装或升级"] --> B["校验配置与维护锁"]
  B --> C["保留暂停选择，统一安全范围"]
  C --> D["每12小时检查编译中间缓存"]
  D --> E["至少7天且无活动、锁、pin或lease"]
  E -->|符合| F["执行前复核后清理"]
  E -->|不符合或未知| G["保留并记录原因"]
  C --> H["任务和模拟器只扫描或待确认"]
```

## 变更记录

- 2026-10-29：按用户要求将本机安全限制推广到正式安装；替代旧自动档删除范围。共享工具缓存迁移保持单独授权。
- CH-01：用户要求“全程后台做”，本轮不打开原生面板做前台点击验收；保留真实未测状态。后端输出、计划、自检和窗口隐藏已验证，不冒称前台验收通过。

## 实现前 AI 总结

AI 转述，非需求真值，仅供对账：默认和升级都应收窄自动删除范围，公开真实清理与缓存命中结果；不得停止活跃任务，不弹面板，不为达空间目标放宽规则。
