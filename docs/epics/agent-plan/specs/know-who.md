# Spec: 看清是谁 v1

## 原始输入

原话全量在 epic 的 [raw-input.md](../raw-input.md)；本 feature 依据 RAW-40～42。

## 意图

资源看板只写进程名和「在用 · 不动」：`node`、`dart`、`Claude` 各占几 GB。看的人不知道它们是哪个项目的、谁起的、该不该还在跑，只好自己去翻 `ps` 和工作目录（RAW-40）。

11-03 在一台 Mac 上查出来：
- 好几个任务各起了一套服务、编译和测试，看板上只是一排同名的 `node`；
- 两路截屏流各占满一个 CPU 核，一路开了 19 小时、一路开了将近 3 天。起它们的两个 agent 会话还开着，所以「父进程已退出」的残留规则认不出来；
- 反过来，agent 用 `nohup … &` 起的后台，父进程马上就退出了，agent 却还在用它。只看「父进程退出、放了一天」会误关。

这些不是这一台 Mac 才有的事（RAW-41）：谁都可能开着几个项目、几个终端、编辑器和 agent，留下忘关的日志流、`--watch` 编译、语言服务和录屏。所以：

- **每个占资源的进程都说清是谁的**：什么工具、哪个项目、谁起的、开了多久，同名的合成一行写份数。
- **「在用」换成看得到的事实**：比如「Cursor 开着」「Claude Code 5 分钟前还在动」「没人管了 · 19 小时」「查不到是谁的」。
- **陪跑后台按主人判断**：只在有人看着时才有用的后台，主人走了、或主人是 agent 且 6 小时没动，才关；主人是还开着的终端或编辑器，不关。
- **后台长时间占满 CPU 要写出来**：只说，不因此关。

## 非目标

- 不读别的进程的环境变量：里面常有密钥。
- 不判断终端、编辑器里的人在不在看：盯着日志的人可能几个小时不打字。
- 不碰 App 本身、数据库、容器、调试器、模拟器本身（沿用 [主动盯着](proactive-watch.md) 的边界）。
- 开发服务器（`vite`、`next dev` 这类）不算陪跑后台：它可能正开在浏览器里，只列出来，不关。

## 验收标准

| ID | 级 | Given | When | Then | 落点 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|
| AC-01 | [核] | 资源看板装好 | 每分钟 | 最占内存的前 12 个进程和长时间占满 CPU 的进程，每个都认出：<br>• **工具**：App 名或程序名（tsc、node…）；<br>• **项目**：工作目录所在的 git 仓库名，不在仓库里就写文件夹名；<br>• **谁起的**：顺着上级进程找到的第一个认得出的主人——终端、编辑器、编程 agent、别的 App；一路找到系统都没有主人的，算「没人管了」；<br>• **开了多久**。<br>同一个工具、同一个主人的合成一组，写份数和各属哪些项目 | CLI后端 | ✅ | auto: test_owners.OwnerTest、WordsTest、ActivityTest；test_watch.CompanionTest.test_the_minute_keeps_who_holds_what_for_the_page_and_the_agent。实测（11-03 18:07，本机）：`watch status` 的 who 写着「Baguette ×2 · 2 个项目 · Claude Code 起的 · 4 分钟前还在动 · 占满 2 个核已经 3 天」「node server.ts · 没人管了 · 45 分钟」「Claude Code ×3 · Claude 开着」「com.apple.WebKit.WebContent · 后台服务」 |
| AC-02 | [核] | 用户打开内存页，或资源看板发「内存在变紧」通知 | 看占内存的那几行 | 每行写 AC-01 的归属，不再一律写「在用 · 不动」。理由写看得到的事实，比如：<br>• 「Cursor 开着」；<br>• 「Claude Code · 5 分钟前还在动」「Codex · 7 小时没动」；<br>• 「没人管了 · 19 小时」；<br>• 「查不到是谁的」。<br>通知里的「最占内存的前三」也带项目和主人 | mac原生、CLI后端 | ✅ | auto: test_dashboard.test_memory_page_says_whose_each_one_is、test_watch.NoticeTest。出图核对（离屏渲染内存页）：每行写主人和动静，「在用 · 不动」不再出现；占满 CPU 的那一句排在最前；封面横幅只写工具名 |
| AC-03 | [核] | 一个陪跑后台在跑。陪跑后台指只在有人看着时才有用的这几类：<br>• 日志流（`log stream`、`tail -f`、`adb logcat`…）；<br>• 画面流和录屏（模拟器截屏流、`simctl io … recordVideo`…）；<br>• `--watch` 模式的编译和测试；<br>• 语言服务、MCP 服务这类编辑器或 agent 的帮手 | 内存变紧，或它自己占了半个核以上 | 按主人决定：<br>• 主人是 agent，那个 agent 6 小时没动 → 关；<br>• 没人管了，开了 6 小时以上，同一个项目里 6 小时内也没有 agent 动过 → 关；<br>• 主人是还开着的终端、编辑器或别的 App → 不关；<br>• 它在往文件里写（在录日志、录屏）→ 不关。<br>关之前复核还是同一个进程。关了的写进通知 | CLI后端 | ✅ | auto: test_owners.VerdictTest 五条（agent 6 小时没动才关；编辑器、终端里的不关；写文件的不关；没人管的要项目里也没 agent 在动；agent 自己的 MCP、语言服务不关）、test_watch.CompanionTest 三条（内存不紧也会关占满一个核的；agent 还在干活就留着）。实测：本机两路截屏流的主人几分钟前还在动，留下了；本机没有满 6 小时的例子，「关掉」这一步由 test_emergency.ProcessStopTest 对真进程验证 |
| AC-04 | [核] | 一个后台连续 30 分钟以上占满大半个核（80% 以上） | 用户打开内存页；或资源看板发「内存在变紧」通知；或叫 agent 来处理告急 | 写出它是谁的、占了几个核、已经多久，比如「截屏流（Claude Code 起的）占满 1 个核已经 3 天」。只写不关，除非 AC-03 允许 | mac原生、CLI后端 | ✅ | auto: test_owners.ActivityTest.test_busy_for_half_an_hour_or_all_its_life、WordsTest。实测：who 和告急材料里都写着「占满 2 个核已经 3 天」，没有因此关掉 |
| AC-05 | [边] | 换一台 Mac：别的终端、编辑器、agent，别的项目结构 | 同 AC-01 | 认得出的主人至少有：<br>• 终端：Terminal、iTerm2、Warp、Ghostty、kitty、Alacritty、WezTerm、tmux；<br>• 编辑器：VS Code、Cursor、Windsurf、Zed、Xcode、JetBrains 全家、Sublime Text；<br>• agent：Claude Code、Codex、Gemini CLI、Aider、opencode。<br>认不出的写「查不到是谁的」，不当作没人管。项目只看 git 仓库，不依赖资源看板的任务目录 | CLI后端 | ✅ | auto: test_owners.OwnerTest（Terminal、iTerm2 + tmux、Cursor、PyCharm、Claude Code、Codex、Aider、Codex App、launchd 服务、root 的系统服务、Xcode 自带的 make）。实测：本机全部进程过一遍，「没人管了」只剩用户自己的残留（开发服务器、代理、agent 帮手）。本机没装 VS Code、Cursor，编辑器只有单元测试覆盖 |

## 变更记录

| ID | 日期 | 变更 | 类型 |
|---|---|---|---|
| CH-01 | 2026-11-03 | 新建（RAW-40～42）。[主动盯着](proactive-watch.md) AC-02 里「截屏流、日志流放一天就关」由本 spec AC-03 取代 | [需求变更] |
| CH-02 | 2026-11-03 | 本机实测时改了几处（[实现引入]）：<br>• 系统自己的服务（系统路径、root 跑的）算后台服务，不算没人管；<br>• App 里自带的命令行工具（Xcode 的 make、git）不算那个 App；<br>• 同名的两份检出写成「a/shop」「b/shop」；<br>• 刚开始盯的 agent 不说「还在动」，看到它动了才说；<br>• 从 `/` 或主目录起的 agent 看不出在哪个项目干活：这时没人管的陪跑后台放满一天才关；<br>• agent 自己的 MCP、语言服务，agent 还在就不关 | [实现引入] |
