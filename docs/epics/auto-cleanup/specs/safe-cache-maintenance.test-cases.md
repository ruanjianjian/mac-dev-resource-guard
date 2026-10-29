# 安全自动维护测试用例

| TC | 对应AC | 前提与操作 | 预期 |
|---|---|---|---|
| TC-01 | AC-01 | 隔离HOME，分别缺少auto_scope/standard/managed-caches；包含闲置、脏、未推送任务、不可用模拟器和旧迁移marker，执行tick | 仅安全范围，任务和marker不变，无simctl delete或整库清理 |
| TC-02 | AC-02 | 真实Mac与隔离cache夹具，执行clean_caches/plan | 只动四类中间目录且至少7天；保护对象保留；目录计量与全盘变化分报 |
| TC-03 | AC-03 | 关闭或扫描中关闭auto_enabled，损坏JSON/非法scope，再恢复 | 不删除；错误回执覆盖旧成功；恢复后可维护 |
| TC-04 | AC-04 | 升级前有storage-paused、持有periodic-storage锁、无效旧task_roots | 保留暂停和旧App，不杀维护；无效配置不启动替换动作 |
| TC-05 | AC-05 | 构建新版App，自检；后台安装，checkup-firstrun/off external/apply caches,clean；同一稳定槽clean build两次 | 版本1.6.1，窗口0，开关与实际一致，两次构建成功并报告真实命中 |
| TC-06 | AC-06 | 后端计划与状态输出检查；Swift自检 | 无自动删任务承诺；前台点击依用户约束未测 |

自动用例入口：`python3 scripts/verify.py`。真实验证步骤与事实见 `docs/validation/1.6.1-safe-maintenance.md`。
