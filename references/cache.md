# App、模拟器与自动化构建的缓存复用

复用单位是**同一工作树 + 工具链 + 平台 + 配置**，而不是每个回归轮次。没有把所有分支的可变编译目录混在一起。

| 内容 | 共享规则 |
|---|---|
| Go GOCACHE/GOMODCACHE、npm cache、pnpm store、pub cache、Gradle 用户下载缓存 | 使用工具原生默认全局位置和并发协议，不给每个任务复制一套 |
| Xcode DerivedData | 同工作树固定槽；不同工作树、Xcode、真机/模拟器/macOS、Debug/Profile/Release 分槽；写入独占跨进程锁 |
| Flutter build/.dart_tool、Pods、node_modules、SwiftPM 可变 checkout | 每工作树独立，不软链接成多任务共写目录 |
| 测试结果、IPA/APK、录像、报告 | 独立于编译缓存保存，保留交付证据；不复制整份 DerivedData 当报告 |
| Gradle build cache、Xcode compilation cache | 本包不全局启用；若项目兼容，可另做命中率验证后接入，不能把潜在节省写成已实现 |

## 直接 Xcode 构建

在真实工作树中：

```bash
dev-storage xcode --worktree "$PWD" --platform ios-simulator --configuration Debug -- \
  xcodebuild -workspace ios/App.xcworkspace -scheme App \
  -configuration Debug -sdk iphonesimulator build
```

示例中的工程/scheme需换成项目实际值；真机改 `ios-device`，macOS 改 `macos`。包装器配置必须与 xcodebuild 实参一致，不另传不同 `-derivedDataPath`。同一配置复测沿用原工作树、原命令，不生成按时间、PID、commit、rXX 命名的新缓存。`DEVELOPER_DIR` 指向系统当前 Xcode 的显式/隐式写法归一化，避免同工具链分叉。

`xcode-path` 只查询，不能凭路径绕过锁写入：

```bash
dev-storage xcode-path --worktree "$PWD" --platform ios-simulator --configuration Debug
```

修改自动化脚本中**真正执行 xcodebuild 的那一行**为上述入口，保留退出码、工作目录、环境和原参数；按脚本当前阶段复用相同槽。`test-without-building` 必须引用对应已构建槽及原 xctestrun；不能把原验收链路悄悄换成另一个包。多阶段脚本可以在自有适配器里调用 `slots.run_in_slot`，整段持锁并使用该槽路径；不要在外层持锁后递归调用同槽包装器。记录实际缓存路径、构建结果和测试结果路径。

验证接入：同工作树同配置连续两次构建，槽 key/path 相同；第二次检查实际编译日志的增量行为。并发相同槽应串行；不同平台/配置应该分开。旧缓存是否删除是另一件事，先保护在途构建和验收。

## 依赖与 Flutter / Android

```bash
dev-storage deps --family pub -- flutter pub get
dev-storage deps --family pods -- pod install
```

在原工作目录执行，短时锁只序列化共享缓存的安装写入，不重定位缓存、不增加空间条件。Flutter 自身 `build/run/test` 继续使用每工作树输出；模拟器属于运行目标，不应每换一台设备就复制完整依赖目录。Android Gradle 保留现有项目缓存兼容配置，不能为了“共享”强行改成所有分支同一 build 目录。

## 自动清理

```bash
dev-storage plan
dev-storage maintain            # 预览
dev-storage maintain --apply    # 获得范围授权后的执行
dev-storage pin /实际工作树 --reason '等待验收'
dev-storage unpin /实际工作树   # 只释放自己的保护
```

默认仅扫描 `~/Library/Caches/dev-resource-guard` 下的受管缓存；不扫整个用户目录，也不自动收养旧 DerivedData。只允许 Intermediates.noindex、Index.noindex、ModuleCache.noindex、SDKStatCaches.noindex 四类目录；保留 Products、源码、报告、设备、登录态和容器。只接受目录，不能当作旧 IPA/ZIP 通用删除器。旧大目录调查与删除走另行明确清单，不扩大自动白名单。

正常保留7天，低于提醒值3天，低于低水位2天；叶目录内文件的最新读写时间、活动进程/打开文件、pin、devlease 和槽锁都优先于年龄。保护名、证据后缀或特殊节点导致整叶保留。通过目录 FD 和 O_NOFOLLOW 防止路径替换越界，未知或部分删除失败停止本轮，不盲目重试。

建议自动化提示：每12小时调用 status，再 maintain --apply，沿用同一入口与锁；busy时退出，不另扫一份。只在明显净回收、低空间升级、失败或需要用户操作时通知。不要因没到目标而阻挡编译或扩大删除。

`free_gib` 来自目标文件系统可用字节，不包含“未来也许可清除”的估计。APFS 克隆共享块和并行构建使目录 du 合计不等于净释放量；需要整盘核对时再读取 diskutil APFS 容器信息。没有安全候选就明确保留，不承诺一定能达到某个剩余量。
