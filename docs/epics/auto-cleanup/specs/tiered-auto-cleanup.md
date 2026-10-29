> 历史 1.4 设计与验收记录。1.6.1 的当前安全范围已由 [安全自动维护](safe-cache-maintenance.md) 替代；下文任务搬迁/裁剪、模拟器自动删除规则不再执行。

# Spec: 自动档清理 v2（基于资源看板 1.3）

## 原始输入

原话全量在 [tiered-auto-cleanup.raw-input.md](./tiered-auto-cleanup.raw-input.md)；首轮 RAW-01～19：需求主体是 RAW-14～16，拍板的选择题回答是 RAW-17～19。本轮新增 RAW-20～21：改到 1.3 基线上。RAW-22～23：确认方案并开始实现。

## 意图

同时开很多任务的开发者，任务目录里的构建产物和放着不用的任务目录越积越多，内置盘几天就被占满（RAW-14、RAW-15）。资源看板 1.3 每 12 小时清一次过期的 Xcode 缓存，但任务目录和模拟器只统计、不处理，也不看剩余空间（RAW-20）。

这个功能接在 1.3 的定时任务上，默认每 30 分钟看一次可用空间（间隔可配置；1.3 的过期缓存清理仍按原来的 12 小时）。低于提醒线时，自动做以下几件不会丢任何东西的事：
- 把闲置的任务目录整个挪到用户配置的外接开发盘；
- 删掉闲置任务里能重新生成的构建产物；
- 清掉系统已判为不可用的模拟器和开发工具自带的缓存。

腾到目标线以上就停。

**用语**：
- **任务目录**：用户在配置里登记的「任务根目录」下的每个子目录，一般一个任务一个，里面是这个任务用到的若干份代码。
- **闲置**：同时满足三条：
  - 没有程序正在里面运行或打开文件；
  - 最近 N 天里面没有文件被改写（N 由用户配置，默认 3 天）；
  - 没有被标记为保护（pin）。
- **外接开发盘**：用户在配置里指定的另一块盘上的目录。
- **自动档开关**：本功能自己的开关，和 1.3 安装时开启的过期缓存清理分开。

## 非目标

- 不删除整个任务目录、不删除正常的模拟器、不关闭开着的模拟器：这些归 [ask-first-confirm](ask-first-confirm.md)。
- 不清理、不搬迁正在使用的任务目录。
- 账号切换 / 可见性：不适用。这是单机单用户工具，没有账号概念。
- 客户端展示名称和图标：不适用。不新增给 agent 用的工具。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 自动清理已打开，内置盘可用空间低于提醒线，配置了外接开发盘且已连接、空间足够，有任务目录闲置满设定天数 | 到了检查时间 | 按闲置时间从久到短，把这些任务目录整个挪到外接开发盘，原位置留一个指向新位置的链接，代码、git 记录、构建和 1.3 生成的项目入口照常能用；可用空间回到目标线以上就停 | CLI后端 | ✅ | auto: test_tiers.test_moves_oldest_idle_first_and_stops_at_target；实测 e2e-tiers.sh（T9 隔离 HOME） 步骤 2：old-idle 真实 ditto 到 T9、原处成链接，git status/HEAD 不变；本机正式安装后，launchd 经 App 运行的定时任务把闲置 3.8 天的 20261022-capsule-process-reply-fold（10.3 GiB）搬到 T9，内置盘可用 9.1 → 19.4 GiB；红: 新功能，基线 b6c7436 无此模块 |
| AC-02 | [核] | 同 AC-01，但没配置外接开发盘或外接盘没连接 | 到了检查时间 | 不搬迁，改为删除这些闲置任务目录里能重新生成的构建产物（编译缓存、依赖安装目录、Flutter 工具缓存等，固定名单），源码、配置文件和不在名单里的文件一个不动；已经挪到外接盘的任务目录，原位置的链接保留不动；看板提示「外接开发盘未连接」 | CLI后端、mac原生 | ✅ | auto: test_without_external_drive_prunes_ignored_outputs_only；实测步骤 3：external 指向未挂载盘，只删 build/、node_modules/，main.swift 保留，回执 external_missing |
| AC-03 | [核] | 某任务目录有程序在里面运行，或设定天数内有文件改写，或被标记为保护 | 到了检查时间 | 这个目录完全不动，构建产物也不删；记录里写明保留原因。有未提交改动但已闲置的目录可以搬迁、可以删构建产物，因为这两样都不会丢源码 | CLI后端 | ✅ | auto: test_in_use_recent_or_pinned_untouched_but_dirty_idle_moves；实测步骤 2：真实 sleep 进程 cwd 在 busy 内 → 保留「PID」；fresh 保留「1.0 天内有改写」 |
| AC-04 | [核] | 搬迁过程中目录里的文件发生变化，或复制前后文件数不一致 | 搬迁该目录 | 放弃这一次，删掉外接盘上的半成品，原目录原样保留，下一轮再试 | CLI后端 | ✅ | auto: test_change_during_copy_aborts_and_leaves_no_copy（复制中写入新文件 → aborted、无残留、无中断标记） |
| AC-05 | [核] | 可用空间低于提醒线，系统里有被标为「不可用」的模拟器（例如它依赖的 iOS 版本已删除），开发工具的缓存在这块盘上 | 到了检查时间 | 删掉不可用的模拟器，用开发工具自带的清理命令清它们的缓存；正常的模拟器一台不动 | CLI后端 | ✅ | auto: test_unavailable_simulators_and_tool_caches；实测：真实 simctl 不可用 0 台未误删；npm 进程在跑时工具缓存跳过 |
| AC-06 | [核] | 可用空间高于提醒线，或自动档开关没打开（安装和从 1.3 升级后的默认状态） | 到了检查时间 | 自动档什么都不删、不搬，1.3 原有的过期缓存清理照常；看板显示「自动档未打开」及打开方法，或显示「空间充足」 | CLI后端、mac原生 | ✅ | auto: test_disabled_or_roomy_does_nothing、test_automatic 缓存 12 小时节奏；实测步骤 1：默认 disabled 零动作 |
| AC-07 | [核] | 上一轮还没跑完，1.3 的过期缓存清理正在跑，或用户正在看板里手动清理 | 新一轮检查触发 | 新一轮直接跳过，同一目录不会被两轮同时处理 | CLI后端 | ✅ | auto: test_automatic.test_tier_step_paces_reports_config_errors_and_yields_to_manual_cleanup（看板操作锁占用 → busy）；与 1.3 共用 periodic-storage 锁 |
| AC-08 | [核] | 自动档处理过若干项 | 打开看板 | 在 1.3 的「自动清理最近结果」旁看到「自动处理记录」：每次处理了哪些目录或缓存、做的是搬迁还是删除、腾出多少空间，以及保留下来的项和原因 | mac原生 | ✅ | auto: test_tier_actions.test_auto_records_show_actions_kept_and_missing_drive；ui(离屏渲染真实后端数据，测试报告动线 J6-tiered-board：自动记录、设置有误、未打开三张截图)；原生窗口按 CH-01 未弹出 |
| AC-09 | [边] | 搬迁到一半时断电、拔盘或进程被杀 | 下一次检查 | 先收拾半成品：原目录还在，就删掉外接盘上不完整的副本；原目录已换成链接、外接盘上副本完整，就算完成。不会出现原目录和链接都不在的情况 | CLI后端 | ✅ | auto: test_recovers_each_interrupted_phase（copying / swapping 两种中断） |
| AC-10 | [边] | 用户改了闲置天数、任务根目录、外接盘路径或检查间隔 | 下一轮检查 | 按新值执行；配置写错（路径不存在、天数不是正数）时自动档这一轮不清理，看板提示哪一项错了。这些设置单独存放，1.3 和更早版本的命令照常读取原有配置，不因新设置报错 | CLI后端、mac原生 | ✅ | auto: test_wrong_values_name_the_field、test_switch_keeps_other_settings_and_old_config_untouched、tick config_error、test_wrong_settings_are_shown_not_hidden |

## 流程图

流程图：[tiered-auto-cleanup.html](tiered-auto-cleanup.html)（给人看，浏览器打开） · 源：[tiered-auto-cleanup.flow.workflow.json](tiered-auto-cleanup.flow.workflow.json)（给 AI 读、可 diff）

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-27 | 新建：闲置天数可配置（RAW-19），自动档四类（RAW-18） | [需求变更] |
| CH-02 | 2026-10-27 | 改以 1.3 为基线：接在 1.3 的定时任务上、默认 30 分钟看空间；自动档单独开关（AC-06）；与 1.3 清理共用锁（AC-07）；记录接在 1.3 看板结果旁（AC-08）；新设置单独存放（AC-10）（RAW-20～21） | [需求变更] |
| CH-03 | 2026-10-28 | [实现引入] 工具缓存只用各自命令（go clean -cache、npm cache verify、pnpm store prune），且只在缓存位于内置盘、该工具没在运行时执行；launchd 每 5 分钟唤醒，过期缓存仍 12 小时一轮 | [实现引入] |
| CH-04 | 2026-10-28 | [实现引入] 本机安装后发现：macOS 静默拒绝后台命令行任务读写外接盘。定时任务改经「资源看板」App 运行，系统询问一次授权；没有授权时如实显示「需要授权」并改删构建产物；读不全的目录不算闲置 | [实现引入] |

## 依赖

- 资源看板 1.3 [自动维护](../../resource-dashboard/specs/automatic-maintenance.md)：本功能复用它的定时任务、锁和看板「接入设置」；过期编译中间文件由它回收。
- [build-slot-wrapper](build-slot-wrapper.md)：整段脚本接入后，构建缓存进 1.3 的受管目录，不再留在任务目录里。
