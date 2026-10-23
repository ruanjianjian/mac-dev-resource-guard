# 资源看板测试用例

先例锚点：`tests/test_portable.py`、`runtime/storage/tests/test_storage.py`、`runtime/memory/tests/test_dev_memory.py`；原生界面使用computer-use操作，非截图渲染器。

| 用例 | AC | 操作/前提 | 观察结果 |
|---|---|---|---|
| disk-preview | AC-01/04 | 安装版磁盘页扫描；等待完成；查看类别和保护原因 | 后台执行且按钮互斥，真实80项全部保留，零真实删除 |
| memory-preview | AC-02/04 | 安装版后台页诊断 | 真实8项应用/任务和1项设备占用保留，无候选禁用 |
| confirm-actions | AC-03/04 | 显式标记fake HOME：扫描、打开确认后取消；再确认；内存fake回执accepted/pending | 取消无clean命令；确认只调用固定后端；旧预览禁用；pending不当stopped |
| global-shortcut | AC-05 | 关闭窗口，Finder中按⌃⌥⌘S；再启动隔离实例制造注册冲突 | 安装版唤回，隔离实例使用⌃⌥⌘R，退出隔离实例释放其组合 |
| missing-test-home | AC-04 | 显式--test-home缺目录、后跟选项或空值 | 拒绝操作，不回退真实工具；Swift self-test及原生缺参窗口验证 |

## 自动化与证伪

`python3 scripts/verify.py`：218项，203通过、15项外部集成按条件跳过。Swift构建后 `ResourceDashboard --self-test` 覆盖状态、单位、失效预览、快捷键回退和缺参隔离拒绝。红：突变移除缺参拒绝后，已实际编译的Swift自测退出1（Self-test failed）；恢复实现自测通过。

以下13条用例均在当前实现通过，并在隔离复制的代码中逐项改坏相应机制后失败；不是声称开发时曾先跑红，也不是用导入/编译失败代替业务失败。复跑命令为 `python3 -m unittest discover -s tests -p test_dashboard.py -v`。

| 用例 | 红：突变 |
|---|---|
| test_classifies_cache_and_keeps_unknown_and_pin | 移除缓存类别白名单 → 单项FAILED，恢复后通过 |
| test_busy_scan_never_reuses_old_plan | 忽略busy回执 → 单项FAILED，恢复后通过 |
| test_incomplete_scan_never_reuses_old_plan | 允许不完整扫描复用旧文件 → 单项FAILED，恢复后通过 |
| test_clean_consumes_preview_and_uses_existing_backend | 不消费已执行预览 → 单项FAILED，恢复后通过 |
| test_expired_or_wrong_token_does_not_call_backend | 延长失效预览可执行窗口 → 单项FAILED，恢复后通过 |
| test_unknown_action_outcome_blocks_replay_even_after_new_preview | 丢失未知动作的防重发状态 → 单项FAILED，恢复后通过 |
| test_paused_memory_disables_reclaim_without_resuming | 忽略守护暂停状态 → 单项FAILED，恢复后通过 |
| test_memory_preserves_apps_devices_and_pending | 泄露top_memory未允许字段 → 单项FAILED，恢复后通过 |
| test_accepted_is_not_reported_as_stopped | 把accepted当退出完成 → 单项FAILED，恢复后通过 |
| test_schema_failures_keep_cleaning_disabled | 吞掉JSON后尾随坏数据 → 单项FAILED，恢复后通过 |
| test_missing_command_has_no_path_fallback | 工具缺失时伪造诊断成功 → 单项FAILED，恢复后通过 |
| test_multiple_window_operation_lock | 移除跨窗口操作锁 → 单项FAILED，恢复后通过 |
| test_subprocess_cli_end_to_end_without_shell | 错误地把空清单标成可清理 → 单项FAILED，恢复后通过 |

测试夹具仅写可销毁目录及记录命令，不关闭真实进程或设备。busy/缺工具/schema错误/保护内容等负向用例与正常路径均执行；绝不将fake回执描述为本机实际释放。
