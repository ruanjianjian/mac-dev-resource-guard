# Design: 分档自动清理（契约 v1）

服务于 [brief](brief.md) 与三份 spec：[tiered-auto-cleanup](specs/tiered-auto-cleanup.md)、[ask-first-confirm](specs/ask-first-confirm.md)、[build-slot-wrapper](specs/build-slot-wrapper.md)。本文冻结跨模块契约（C1～C6），多人并行实现时以本文为唯一真源；改契约先改本文并升版本号，再通知受影响的实现者。

## 模块与归属

| 模块 | 文件 | 归属 |
|---|---|---|
| 新设置读写与校验 | `runtime/storage/tiers_config.py` | 后端 |
| 任务目录盘点、闲置判断、git 状态 | `runtime/storage/tasks.py` | 后端 |
| 自动档执行（挪目录、删产物、不可用模拟器、工具缓存） | `runtime/storage/auto_tier.py` | 后端 |
| 询问档候选、待确认队列、执行与通知请求 | `runtime/storage/ask_tier.py` | 后端 |
| 定时入口 | `scripts/service.py`（`tick-storage` 内接入，见 C5） | 后端 |
| 看板动作 | `widget/backend.py`、`widget/setup_status.py` | 后端 |
| 看板界面、红点、系统通知 | `widget/*.swift` | 界面 |
| 整段脚本接入 | `runtime/storage/cli.py`、`runtime/storage/slots.py` | 构建 |

## C1 新设置文件

路径 `~/.config/dev-resource-guard/auto-cleanup.json`（与 1.3 的 `config.json` 分开，旧命令读不到它）。不存在时等同全部默认值。

```json
{
  "schema": 1,
  "auto_enabled": false,
  "ask_enabled": true,
  "check_interval_minutes": 30,
  "task_roots": [
    {"path": "/Users/me/work/worktrees", "external": "/Volumes/T9/work/worktrees"}
  ],
  "auto_idle_days": 3,
  "ask": {"simulator_unused_days": 7, "simulator_unattached_hours": 4, "task_idle_days": 7},
  "prune_names": []
}
```

- `auto_enabled` 默认 false（spec AC-06）；`ask_enabled` 默认 true。
- `task_roots[].external` 可省略；省略或该路径所在卷未挂载时，自动档对该根走「删产物」分支（tiered AC-02）。
- 整数项必须为正；路径必须是绝对路径。`task_roots[].path` 不存在、`external` 指向主目录 / 卷根 / 本工具目录都算写错：这一轮自动档与询问档都不执行，错误写进 C3 回执的 `config_error`（tiered AC-10）。
- `prune_names` 追加到内置名单（C2），不能删减内置项。
- 未知字段报错，不静默忽略。

## C2 任务目录与可删产物

- **任务目录**：每个 `task_roots[].path` 的直接子目录，含已是指向外接盘的链接的子目录（链接本身算已搬迁）。
- **闲置**（自动档 N = `auto_idle_days`，询问档 N = `ask.task_idle_days`）同时满足：
  - 没有进程的 cwd、打开的文件、命令行落在该目录内（复用 `storage.Activity`；采集失败按「在用」处理）；
  - 最近 N 天没有文件改写（遍历时跳过 C2 名单内的产物目录和 `.git/objects`）；
  - 没有被 pin（复用 dev-storage 的 pin 记录，按路径前缀匹配）。
- **git 状态**：对目录内每个 git 工作区跑 `git status --porcelain` 与「本地有、上游没有的提交」检查；任何一个有未提交或未推送改动即记为「有改动」。无上游的分支算「未推送」。
- **可删产物内置名单**：`build`、`.dart_tool`、`node_modules`、`DerivedData`、`.gradle`、`Pods`、`.build`、`target`、`.next`、`.turbo`、`.venv`、`.mobile-v`。只删同时满足以下条件的：名字在名单内；位于某个 git 工作区内；`git check-ignore` 判定为被忽略；路径上没有链接。不在 git 工作区内的一律不删。

## C3 自动档回执

- 每轮覆盖写 `~/.local/state/dev-resource-guard/auto-tier.json`，同时追加一行到 `auto-tier-log.jsonl`（保留最近 2000 行）。

```json
{"schema": 1, "at": 1793110000.0, "status": "complete",
 "free_before": 13.2, "free_after": 41.0, "target_free_gib": 92.1, "warning_free_gib": 69.1,
 "config_error": null, "external_missing": ["/Volumes/T9/work/worktrees"],
 "actions": [{"kind": "move", "path": "...", "dest": "...", "bytes": 1234, "result": "done", "reason": "闲置 5 天"}],
 "kept": [{"path": "...", "reason": "有进程在用"}]}
```

- `status`：`disabled`（自动档未打开）/ `healthy`（空间高于提醒线）/ `complete` / `partial`（部分动作失败）/ `busy`（与 1.3 清理或手动清理撞锁，本轮跳过）/ `config_error`。
- `actions[].kind`：`move` / `prune` / `simulator-delete`（仅不可用模拟器）/ `tool-cache`。`result`：`done` / `failed` / `aborted`（搬迁中文件变化，已回滚）。
- 搬迁过程状态写 `auto-tier-move.json`（`{src, dest, phase: copying|swapping}`），用于崩溃后收拾（tiered AC-09）。

## C4 待确认队列

文件 `~/.local/state/dev-resource-guard/pending.json`，只由后端写。

```json
{"schema": 1, "updated": 1793110000.0,
 "items": [{"id": "simulator-delete:8F1C…", "kind": "simulator-delete", "title": "Elys-Old（iPhone 16，iOS 26.0）",
            "bytes": 4823449600, "reason": "已关机 12 天没开过", "loses": "上面装的 App 和登录状态",
            "state": "pending", "fingerprint": "…", "result": null, "updated": 1793110000.0}],
 "notify": {"last_day": "2026-10-27", "last_critical_at": null}}
```

- `kind`：`simulator-delete` / `simulator-shutdown` / `task-delete` / `runtime-delete`。`id` = `kind:对象标识`（模拟器 UDID、运行时标识、任务目录绝对路径），跨轮稳定。
- `state`：`pending` → `running` → `done` / `failed`；`pending` → `ignored`。`ignored` 的项在 `fingerprint`（对象状态摘要，例如模拟器最近启动时间、目录最新改写时间）变化后回到 `pending`（ask AC-03）。`failed` 的项保留在列表，可再次确认（ask AC-07）。
- 每轮重新扫描：不再满足条件的 `pending` 项删除；永不档对象不进入（ask AC-05）。

## C5 看板动作（`widget/backend.py`）

沿用现有调用方式：`python3 backend.py <action> [--token T] [--path P] [--ids a,b]`，输出 JSON `{status, message, rows, token?, created?}`。新增动作：

| 动作 | 作用 | 返回 |
|---|---|---|
| `pending-preview` | 读 C4，返回 `state` 为 pending / failed / ignored 的项 | `rows` + 5 分钟有效、一次性的 `token` |
| `pending-clean --token T --ids a,b` | 只执行勾选的 id；逐项按 C4 条件重新核对，不过即跳过 | 逐项 `rows`，`outcome` 为「已完成 / 已跳过 / 失败」+ 原因 |
| `pending-ignore --ids a,b` | 标记忽略 | 新的 `rows` |
| `auto-preview` | 读 C3 最近回执和日志 | 「自动处理记录」`rows` |
| `setup-auto-enable` / `setup-auto-disable` | 写 C1 的 `auto_enabled` | `setup_status` |

- 行（row）在现有字段之外增加两个可选字段：`kind`（同 C4）和 `loses`（会失去什么）。界面用 `eligible` 表示可勾选。
- 执行勾选项时与 1.3 清理共用 `periodic-storage` 锁；拿不到锁返回 `status: busy`，不执行任何一项（ask AC-06）。

## C6 通知请求与红点

- 后端需要提醒时写 `~/.local/state/dev-resource-guard/notify-request.json`：`{"at": …, "count": 3, "reason": "daily|critical"}`。
- 节流（ask AC-04）：
  - 同一天最多写一次 `daily`；
  - 可用空间跌破低水位时写一次 `critical`，回到低水位以上之后才会再写。
- 看板没在运行时，后端执行 `open -g -j <资源看板.app>`，让它在后台启动。
- 看板每次采样（15 秒）检查请求文件：
  - 有请求就发一条系统通知，标题「有 N 项清理等你确认」，然后删除请求文件；
  - 点通知打开面板，并切到「待确认」。
- 菜单栏红点只看 `pending.json` 里 `state` 为 pending 的数量是否大于 0。

## 定时入口（`service.py tick-storage`）

1. 1.3 原有的两类缓存清理：按 1.3 的节奏，上次完成满 12 小时才跑。
2. 读 C1。`config_error` → 写 C3 回执后结束本轮（不影响第 1 步）。
3. 自动档：`auto_enabled` 且可用空间低于提醒线时，按闲置从久到短执行，回到目标线就停。
4. 询问档：`ask_enabled` 时每轮刷新 C4，按 C6 节流写通知请求。
5. 1.3 的 launchd 间隔从 12 小时改为 `check_interval_minutes`（默认 30 分钟）；升级时由 `install.py` 重写我方标签（沿用 1.3 的迁移流程，不动外部调度）。

## 构建：整段脚本模式（build-slot-wrapper）

```
.dev-resource-guard/xcode-build --platform ios-simulator --configuration Debug \
    --cache-env ELYS_SIMULATOR_DERIVED_DATA_PATH -- ./tool/run_ios_simulator.sh build
```

- 带 `--cache-env NAME` 时，不往参数里注入 `-derivedDataPath`；改为把固定槽的 DerivedData 路径放进环境变量 NAME，再运行 `--` 之后的整段命令。
- 锁、清单、`last_used` 和 1.3 相同。整段命令退出码为 0 时 `successful_builds` 加 1（不再要求参数里出现 xcodebuild 动作）。
- 缓存根在 `/Volumes/<盘名>/` 下而 `/Volumes/<盘名>` 不是挂载点时，报错「缓存所在的盘未连接：<路径>」，退出码 2，不建目录。带不带 `--cache-env` 都做这项检查。
- 不带 `--cache-env` 时，行为与 1.3 完全一致（AC-06）。
