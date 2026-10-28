# Spec: 整段构建脚本接入项目入口 v2（基于资源看板 1.3）

## 原始输入

原话全量在 [build-slot-wrapper.raw-input.md](./build-slot-wrapper.raw-input.md)；首轮 RAW-01～05：需求来自 RAW-05 里的追问「我们的构建缓存共享的能力加进来了吗」。本轮新增 RAW-06～07：改到 1.3 基线上。RAW-08～09：确认方案并开始实现。

## 意图

同一个任务反复构建 iOS App 时，很多项目的构建脚本把编译缓存放在各自任务目录里，工具管不到，旧的编译中间文件就一直堆着（RAW-05）。

资源看板 1.3 已经提供「项目入口」：项目接入后生成 `.dev-resource-guard/xcode-build`，通过它跑的 xcodebuild 固定使用这个工作树的一份缓存，过期中间文件由 1.3 的定时清理回收（RAW-06）。但它只能包住一条 xcodebuild 命令。Flutter 这类项目的构建脚本要先装依赖、生成工程，再自己调用 xcodebuild；不改脚本就接不进来。

这个功能让同一个项目入口也能包住一整段构建脚本：脚本在这个工作树固定的那份缓存里运行，并通过用户指定的环境变量知道缓存在哪。缓存在 1.3 管理的目录里，这个目录也可以配到外接盘。

## 非目标

- 不修改具体项目的构建脚本：只要脚本已支持用环境变量指定缓存位置，就能直接接入。各项目怎么接入，写在各自的接入说明里。
- 不在不同任务之间共用同一份可变的编译缓存。
- 不把以前散落在各任务目录里的旧缓存迁进来。
- 账号切换 / 可见性、客户端展示名称和图标：不适用。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 项目已在 1.3 接入；指定了平台、配置，以及脚本读缓存位置用的环境变量名 | 用项目入口运行一整段构建脚本 | 脚本读到的缓存位置指向这个工作树固定的那一份；同一工作树、同一配置连续构建两次，用的是同一份缓存，第二次是增量编译；脚本成功结束才算一次成功构建，看板「项目构建缓存」随之计数 | CLI后端、mac原生 | ✅ | auto: test_script_slot.test_script_reads_the_same_slot_and_successes_count；实测：Swift 包经入口脚本模式构建两次，同一槽路径，第 1 次 7 秒 4 个编译步骤、第 2 次 1 秒 0 步，successful_builds=2；红: 新功能，基线 b6c7436 无此模块 |
| AC-02 | [核] | 同一份缓存正在被一次构建使用 | 另一次构建也要用它 | 后一次排队，等前一次结束才开始，不会同时写；不同任务或不同配置互不等待 | CLI后端 | ✅ | auto: test_same_slot_queues_other_configuration_runs_alongside |
| AC-03 | [核] | 构建脚本正常结束、失败或被中断（Ctrl-C、超时被杀） | 项目入口返回 | 退出码和输出跟直接运行脚本一致；锁会释放，下次能正常使用 | CLI后端 | ✅ | auto: test_exit_code_output_and_interrupt_pass_through_and_release_lock（退出码 7、stdout/stderr 原样；进程组 Ctrl-C 后锁立即可用） |
| AC-05 | [边] | 受管缓存目录配到了外接盘，而外接盘没连接 | 用项目入口构建 | 报出明确错误「缓存所在的盘未连接：<路径>」，不悄悄在内置盘另建一份缓存 | CLI后端 | ✅ | auto: test_unplugged_cache_drive_is_a_clear_error；实测：cache_root=/Volumes/NoSuchGuardDrive → 「缓存所在的盘未连接」、退出码 2、未建目录 |
| AC-06 | [核] | 项目在 1.3 已接入，仍按 1.3 的写法用项目入口跑一条 xcodebuild | 构建 | 行为与 1.3 一致：同一份缓存、同样计数；已接入的项目不用重新接入 | CLI后端 | ✅ | auto: test_bad_variable_name_rejected_and_13_form_unchanged（仍注入 -derivedDataPath，-list 不计数） |

## 流程图

流程图：[build-slot-wrapper.html](build-slot-wrapper.html)（给人看，浏览器打开） · 源：[build-slot-wrapper.flow.workflow.json](build-slot-wrapper.flow.workflow.json)（给 AI 读、可 diff）

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-10-27 | 新建：把构建缓存共享接入实际的构建脚本（RAW-05） | [需求变更] |
| CH-02 | 2026-10-27 | 改以 1.3 为基线：扩展 1.3 的项目入口而不是另做包装命令；删除 AC-04（过期中间文件回收已由 1.3 AC-AUTO-02 覆盖）；新增 AC-06（1.3 写法不变）（RAW-06～07） | [需求变更] |
| CH-03 | 2026-10-28 | [实现引入] 只杀包装命令而构建进程还在跑时，锁随构建真正结束才释放（沿用 1.3，避免两次构建同时写一份缓存）；Ctrl-C 作用于整个进程组时锁立即释放 | [实现引入] |

## 依赖

- 资源看板 1.3 [自动维护](../../resource-dashboard/specs/automatic-maintenance.md)：项目入口、固定缓存、锁、成功构建计数和过期中间文件回收都沿用它。
