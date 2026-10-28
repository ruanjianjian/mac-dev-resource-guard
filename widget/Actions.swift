import AppKit
import SwiftUI

struct ResourceRow: Decodable, Identifiable {
    let id: String
    let title: String
    let detail: String
    let category: String
    let bytes: Int64?
    let eligible: Bool
    let reason: String
    let outcome: String?
    let command: String?
    let kind: String?
    let loses: String?
}

struct BackendReply: Decodable {
    let status: String
    let message: String
    let rows: [ResourceRow]?
    let token: String?
    let created: Double?
    let eligible: Int?
    let eligibleBytes: Int64?
    var canClean: Bool {
        status == "ok" && token != nil && (eligible ?? 0) > 0 &&
        created.map { (0...300).contains(Date().timeIntervalSince1970 - $0) } == true
    }
}

func byteCount(_ bytes: Int64) -> String {
    ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
}

func testHomeArgument(_ arguments: [String]) throws -> String? {
    guard let index = arguments.firstIndex(of: "--test-home") else { return nil }
    guard index + 1 < arguments.count, !arguments[index + 1].hasPrefix("--"), !arguments[index + 1].isEmpty else {
        throw NSError(domain: "Dashboard", code: 2, userInfo: [NSLocalizedDescriptionKey: "隔离测试参数缺少目录，拒绝回退到真实工具。"])
    }
    return arguments[index + 1]
}

final class DashboardActions: ObservableObject {
    /// One synchronous backend call; the panel runs it off the main thread.
    static func call(_ action: String, token: String? = nil, path: String? = nil, ids: [String]? = nil) throws -> BackendReply {
        guard let script = Bundle.main.url(forResource: "backend", withExtension: "py") else {
            throw NSError(domain: "Dashboard", code: 1, userInfo: [NSLocalizedDescriptionKey: "应用缺少后端适配器，请重新安装。"])
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        var args = [script.path, action]
        if let token { args += ["--token", token] }
        if let path { args += ["--path", path] }
        if let ids { args += ["--ids", ids.joined(separator: ",")] }
        // An explicit, marked disposable home is only for UI integration tests.
        if let home = try testHomeArgument(CommandLine.arguments) {
            guard FileManager.default.fileExists(atPath: home + "/.resource-dashboard-fixture") else {
                throw NSError(domain: "Dashboard", code: 2, userInfo: [NSLocalizedDescriptionKey: "隔离测试目录缺少标记，拒绝回退到真实工具。"])
            }
            args += ["--home", home]
            var env = ProcessInfo.processInfo.environment
            env["HOME"] = home
            task.environment = env
        }
        task.arguments = args
        let pipe = Pipe(); task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        try task.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        guard task.terminationStatus == 0 else {
            throw NSError(domain: "Dashboard", code: 3, userInfo: [NSLocalizedDescriptionKey: "后端连接失败，请检查工具安装。"])
        }
        return try JSONDecoder().decode(BackendReply.self, from: data)
    }

    @Published var busy = false
    @Published var activity = ""
    @Published var message = ""
    @Published var lastStatus = ""
    @Published var previews: [String: BackendReply] = [:]
    /// A notification click asks the panel to open a tab.
    @Published var requestedTab: String?

    func run(_ family: String, clean: Bool = false, action: String? = nil, path: String? = nil, ids: [String]? = nil) {
        guard !busy, ["storage", "common", "memory", "setup", "task", "project", "pending", "auto"].contains(family) else { return }
        let preview = previews[family]
        if clean && preview?.canClean != true {
            message = "请先重新扫描，查看本次预览。"; return
        }
        let token = clean ? preview?.token : nil
        previews[family] = nil  // Neither failed requests nor old results enable cleaning.
        busy = true; message = ""
        activity = clean ? "后端正在重新核对保护并执行，请等待完成…" :
            (["storage", "common"].contains(family) ? "正在扫描缓存及占用；大目录可能需要几分钟…" : "正在检查，请稍候…")
        let command = action ?? (family + (clean ? "-clean" : "-preview"))
        DispatchQueue.global(qos: .utility).async {
            let reply: BackendReply
            do {
                reply = try DashboardActions.call(command, token: token, path: path, ids: ids)
            } catch {
                reply = BackendReply(status: "error", message: clean && (error as NSError).code == 3 ? "后端连接中断，操作结果待核实；不会自动重试。" : error.localizedDescription,
                                     rows: nil, token: nil, created: nil, eligible: nil, eligibleBytes: nil)
            }
            DispatchQueue.main.async {
                self.previews[family == "project" ? "setup" : family] = reply
                self.message = reply.message; self.lastStatus = reply.status
                self.busy = false; self.activity = ""
                (NSApp.delegate as? AppDelegate)?.model.refresh()
            }
        }
    }
}

struct Dashboard: View {
    @ObservedObject var model: Monitor
    @ObservedObject var actions: DashboardActions
    @State private var tab = "overview"
    @State private var confirming = false
    @State private var storageFamily = "common"
    @State private var setupAction = ""
    @State private var confirmingSetup = false
    @State private var selected: Set<String> = []
    @State private var confirmingPending = false

    init(model: Monitor, actions: DashboardActions, initialTab: String = "overview", initialSelected: Set<String> = []) {
        _model = ObservedObject(wrappedValue: model); _actions = ObservedObject(wrappedValue: actions)
        _tab = State(initialValue: initialTab); _selected = State(initialValue: initialSelected)
    }

    private var family: String { tab == "storage" ? storageFamily : tab }
    private var preview: BackendReply? { actions.previews[family] }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(CommandLine.arguments.contains("--test-home") ? "资源看板 · 隔离验证" : "资源看板").font(.title2.weight(.semibold))
                Spacer()
                Text("\(model.snapshot.pressureLabel) · 可用 \(number(model.snapshot.free)) GiB")
                    .font(.callout).foregroundColor(.secondary)
                Button {
                    model.pinned.toggle()
                    UserDefaults.standard.set(model.pinned, forKey: "pinned")
                    (NSApp.delegate as? AppDelegate)?.window.level = model.pinned ? .floating : .normal
                } label: { Image(systemName: model.pinned ? "pin.fill" : "pin") }
                    .buttonStyle(.borderless)
                    .accessibilityLabel(model.pinned ? "取消置顶" : "置顶窗口")
            }.padding(.horizontal, 22).padding(.top, 18)
            Picker("查看", selection: $tab) {
                Text("实时概览").tag("overview")
                Text("磁盘缓存").tag("storage")
                Text("开发后台").tag("memory")
                Text(model.snapshot.pending > 0 ? "待确认 \(model.snapshot.pending)" : "待确认").tag("pending")
                Text("自动记录").tag("auto")
                Text("接入设置").tag("setup")
            }.pickerStyle(.segmented).padding(18)
            if tab == "overview" {
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("先看占用，再决定清理").font(.headline)
                        Text("完整安装会开启安全自动维护；在接入设置查看运行结果与构建缓存复用。")
                            .font(.callout).foregroundColor(.secondary)
                        HStack {
                            Button("查看已有缓存") { tab = "storage" }
                            Button("检查接入与自动维护") { tab = "setup" }
                        }
                    }.padding(.horizontal, 24)
                    Overview(model: model).frame(maxWidth: .infinity)
                }
            } else {
                if tab == "setup" { setupControls } else if tab == "pending" { pendingControls } else if tab == "auto" { autoControls } else { controls }
                Divider()
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if tab == "pending", let reply = preview, reply.token != nil, let rows = reply.rows, !rows.isEmpty {
                            PendingList(rows: rows, selected: $selected)
                        } else if let reply = preview, let rows = reply.rows, !rows.isEmpty {
                            ForEach(Array(Set(rows.map(\.category))).sorted(), id: \.self) { category in
                                ResourceGroup(title: category, rows: rows.filter { $0.category == category })
                            }
                        } else if !actions.busy {
                            VStack(alignment: .leading, spacing: 10) {
                                Text(preview == nil ? "先查看，再决定" : "本次没有可展示的项目")
                                    .font(.title3.weight(.medium))
                                Text(tab == "storage" ? "首次使用请选择已有缓存并扫描，无需改构建脚本。可清理项、大项占用与保留原因会分别显示。"
                                     : tab == "pending" ? "会丢东西的清理（删模拟器、关模拟器、删任务目录、删运行时）先放在这里，你勾选确认后才执行。"
                                     : tab == "auto" ? "自动档每次挪了哪些目录、删了哪些构建产物、为什么保留，都记在这里。"
                                     : "诊断后台归属与闲置证据，正在使用或无法核实的资源保留。")
                                    .foregroundColor(.secondary)
                            }.padding(.vertical, 35)
                        }
                    }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if actions.busy {
                HStack(spacing: 10) { ProgressView().controlSize(.small); Text(actions.activity).font(.callout) }
                    .padding(16).frame(maxWidth: .infinity, alignment: .leading)
            }
            if !actions.message.isEmpty {
                Text(actions.message).font(.callout)
                    .foregroundColor(["error", "unknown"].contains(actions.lastStatus) ? .orange : .secondary)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    .padding(16).frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            HStack {
                Text("唤起 \(model.shortcut) · 自动刷新仅采样状态").font(.caption).foregroundColor(.secondary)
                Spacer()
                Button("刷新状态") { model.refresh() }.buttonStyle(.borderless)
            }.padding(16)
        }
        .frame(width: 620, height: 700)
        .background(Color(nsColor: .windowBackgroundColor))
        .onChange(of: tab) { value in
            selected = []
            if ["setup", "pending", "auto"].contains(value) && !actions.busy { actions.run(value) }
        }
        .onReceive(actions.$requestedTab) { value in
            if let value { tab = value; actions.requestedTab = nil; if !actions.busy { actions.run(value) } }
        }
        .alert("执行勾选的 \(selected.count) 项？", isPresented: $confirmingPending) {
            Button("取消", role: .cancel) {}
            Button("确认执行") { actions.run("pending", action: "pending-clean", ids: Array(selected)); selected = [] }
        } message: {
            Text(pendingSummary)
        }
        .alert(tab == "storage" ? "按当前规则清理缓存？" : "安全回收闲置开发后台？", isPresented: $confirming) {
            Button("取消", role: .cancel) {}
            Button("确认清理") { actions.run(family, clean: true) }
        } message: {
            Text(tab == "storage"
                 ? "预览有 \(preview?.eligible ?? 0) 项符合条件，目录统计约 \(byteCount(preview?.eligibleBytes ?? 0))。已有缓存仅处理本次预览中的项目，执行前复核占用和变化；固定槽沿用现有工具规则。保留 Products、验收包、报告和设备数据；下次编译可能变慢。"
                 : "预览有 \(preview?.eligible ?? 0) 项符合闲置条件。后端仍会复核身份、占用、冷却与待核实请求；仅通过复核的后台正常退出。不会恢复已暂停的守护。")
        }
        .alert("配置资源维护？", isPresented: $confirmingSetup) {
            Button("取消", role: .cancel) {}
            Button("确认") { actions.run("setup", action: setupAction) }
        } message: {
            Text(setupAction == "setup-activate"
                 ? "安装完整引擎并开启自动维护：立即开始并每12小时清理符合规则的已有 Xcode 中间缓存和固定构建槽；每分钟观察内存，只收尾有闲置证据的后台。复用已有守护，保留原命令、pin、设备和任务。自动清理不逐次弹窗；可随时关闭本工具调度。"
                 : setupAction == "setup-upgrade"
                 ? "只升级可识别的本工具旧版本，先备份并校验再切换，保留配置、暂停状态和保护记录。运行中的维护、托管后台、本地修改或其他工具会阻止升级；不会停止它们。"
                 : setupAction == "setup-install"
                 ? "安装内置 dev-storage / dev-memory 命令。已有不同实现会保留并提示冲突，不覆盖状态或保护记录。安装本身不会开启自动清理。"
                 : setupAction.hasPrefix("setup-auto")
                 ? (setupAction.hasSuffix("enable") ? "打开自动档：空间低于提醒线时，自动把闲置的任务目录整个挪到外接盘（原路径留链接），外接盘不在就删可重新生成的构建产物，再清不可用的模拟器和工具缓存。有未提交改动的目录只挪不删；正在用、被保护的不动。打开一次即算授权，之后不再逐项询问。" : "关闭自动档：不再自动搬迁或删构建产物，已挪走的目录和链接保持原样。")
                 : setupAction.hasPrefix("setup-ask")
                 ? (setupAction.hasSuffix("enable") ? "打开询问档：发现久未使用的模拟器、闲置且已推送的任务目录、没人用的运行时时，发通知请你在「待确认」里勾选确认，不会自动执行。" : "关闭询问档：不再扫描和通知；已列出的待确认项保留，可手动处理。")
                 : "\(setupAction.contains("enable") ? "开启" : "暂停")\(setupAction.contains("memory") ? "每分钟观察、满足安全条件才回收的内存维护" : "每 12 小时处理固定槽与已有 Xcode 过期中间缓存的维护")。暂停不杀在途工作，下一候选复核开关；未接入协议的模拟器、MCP 和任务目录保持保留。")
        }
    }

    private var setupControls: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("安装、自动维护与缓存复用").font(.headline)
            Text("完整安装一次开启安全维护；旧命令保留，已有守护不会重复启动。")
                .font(.callout).foregroundColor(.secondary)
            HStack {
                Button("检查接入") { actions.run("setup") }
                Button("完成安装并开启维护") { configure("setup-activate") }
                Button("接入项目构建缓存") {
                    let panel = NSOpenPanel()
                    panel.canChooseDirectories = true; panel.canChooseFiles = false
                    panel.allowsMultipleSelection = false
                    panel.message = "选择项目根目录，将创建 .dev-resource-guard/xcode-build；已有构建脚本和源码不改写。"
                    if panel.runModal() == .OK, let url = panel.url {
                        actions.run("project", action: "project-integrate", path: url.path)
                    }
                }
            }
            HStack {
                Text("内存自动回收"); Spacer()
                Button("开启") { configure("setup-memory-enable") }
                Button("关闭") { configure("setup-memory-disable") }
            }
            HStack {
                Text("磁盘自动维护"); Spacer()
                Button("开启") { configure("setup-storage-enable") }
                Button("关闭") { configure("setup-storage-disable") }
            }
            HStack {
                Text("自动档（搬闲置目录、删构建产物）"); Spacer()
                Button("开启") { configure("setup-auto-enable") }
                Button("关闭") { configure("setup-auto-disable") }
            }
            HStack {
                Text("询问档（先通知你确认）"); Spacer()
                Button("开启") { configure("setup-ask-enable") }
                Button("关闭") { configure("setup-ask-disable") }
            }
        }.disabled(actions.busy).padding(.horizontal, 20).padding(.bottom, 16)
    }

    private var pendingRows: [ResourceRow] { preview?.rows?.filter { selected.contains($0.id) } ?? [] }

    private var pendingSummary: String {
        let bytes = pendingRows.reduce(Int64(0)) { $0 + ($1.bytes ?? 0) }
        let losses = Set(pendingRows.compactMap(\.loses)).sorted().joined(separator: "；")
        return "约 \(byteCount(bytes))。会失去：\(losses)。执行前逐项再核对，状态变了的会跳过。"
    }

    private var pendingControls: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text("会丢东西的清理，等你确认").font(.headline)
                    Text("已勾选 \(selected.count) 项").font(.callout).foregroundColor(.secondary)
                }
                Spacer()
                Button("刷新列表") { selected = []; actions.run("pending") }
                Button("忽略所选") { actions.run("pending", action: "pending-ignore", ids: Array(selected)); selected = [] }
                    .disabled(selected.isEmpty)
                Button("确认执行所选") { confirmingPending = true }
                    .buttonStyle(.borderedProminent)
                    .disabled(selected.isEmpty || preview?.canClean != true)
            }
            Text("忽略后不再提醒，直到它的状态变化。确认 5 分钟内有效；有未提交或未推送改动、正在用的、被保护的不会出现在这里。")
                .font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
        }.disabled(actions.busy).padding(.horizontal, 20).padding(.bottom, 16)
    }

    private var autoControls: some View {
        HStack {
            Text("只做不丢东西的清理：搬到外接盘、删可重新生成的构建产物、清不可用模拟器和工具缓存。")
                .font(.callout).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
            Spacer()
            Button("刷新记录") { actions.run("auto") }.disabled(actions.busy)
        }.padding(.horizontal, 20).padding(.bottom, 16)
    }

    private func configure(_ action: String) {
        setupAction = action; confirmingSetup = true
    }

    private var controls: some View {
        VStack(alignment: .leading, spacing: 12) {
            if tab == "storage" {
                Picker("缓存范围", selection: $storageFamily) {
                    Text("已有缓存与大项占用").tag("common")
                    Text("固定构建槽").tag("storage")
                }.pickerStyle(.segmented).disabled(actions.busy)
                if storageFamily == "common" {
                    Button("选择任务目录（只统计，不删除）") {
                        let panel = NSOpenPanel()
                        panel.canChooseDirectories = true; panel.canChooseFiles = false
                        panel.allowsMultipleSelection = false
                        if panel.runModal() == .OK, let url = panel.url {
                            actions.run("task", action: "task-select", path: url.path)
                        }
                    }.disabled(actions.busy)
                }
            }
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text(tab == "storage" ? "构建缓存" : "后台回收预览").font(.headline)
                    if let reply = preview, reply.status == "ok", reply.token != nil {
                        Text("\(reply.eligible ?? 0) 项符合条件 · \((reply.rows?.count ?? 0) - (reply.eligible ?? 0)) 项保留")
                            .font(.callout).foregroundColor(.secondary)
                    } else if let reply = preview, reply.status == "ok" {
                        Text("操作已结束 · 再次扫描可查看新预览").font(.callout).foregroundColor(.secondary)
                    }
                }
                Spacer()
                Button(tab == "storage" ? "扫描缓存" : "诊断后台") { actions.run(family) }
                    .disabled(actions.busy)
                Button(tab == "storage" ? "清理规则内缓存" : "安全回收") { confirming = true }
                    .buttonStyle(.borderedProminent)
                    .disabled(actions.busy || preview?.canClean != true)
            }
            Text(tab == "storage"
                 ? "已有缓存无需接入构建脚本；仅清理四类中间目录。大项占用不计入可清理量。"
                 : "模拟器回收仅关机；MEM 是进程记账，不能相加当作可释放内存。")
                .font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
            if let created = preview?.created {
                Text("预览于 \(Date(timeIntervalSince1970: created).formatted(date: .omitted, time: .standard)) · 5 分钟内有效")
                    .font(.caption).foregroundColor(.secondary)
            }
        }.padding(.horizontal, 20).padding(.bottom, 16)
    }
}

private struct PendingList: View {
    let rows: [ResourceRow]
    @Binding var selected: Set<String>
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(rows) { row in
                HStack(alignment: .top, spacing: 10) {
                    Toggle("", isOn: Binding(get: { selected.contains(row.id) },
                                             set: { on in if on { selected.insert(row.id) } else { selected.remove(row.id) } }))
                        .toggleStyle(.checkbox).labelsHidden().disabled(!row.eligible)
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(row.title).fontWeight(.medium).lineLimit(1)
                            Spacer()
                            if let bytes = row.bytes, bytes > 0 { Text(byteCount(bytes)).monospacedDigit().foregroundColor(.secondary) }
                            Text(row.outcome ?? "待确认").font(.caption)
                                .foregroundColor(row.outcome == "上次失败" || row.outcome == "外接盘未连接" ? .orange : .secondary)
                        }
                        Text("为什么：" + row.reason).font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                        if let loses = row.loses { Text("会失去：" + loses).font(.caption).foregroundColor(.orange) }
                    }
                }.padding(.vertical, 9)
                Divider()
            }
        }
    }
}

private struct ResourceGroup: View {
    let title: String
    let rows: [ResourceRow]
    @State private var expanded = true
    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            ForEach(rows.sorted { $0.eligible && !$1.eligible }) { row in
                VStack(alignment: .leading, spacing: 5) {
                    HStack {
                        Image(systemName: row.eligible ? "checkmark.circle" : "lock")
                            .foregroundColor(row.eligible ? .green : .secondary)
                        Text(row.title).fontWeight(.medium).lineLimit(1)
                        Spacer()
                        if let bytes = row.bytes { Text(byteCount(bytes)).monospacedDigit().foregroundColor(.secondary) }
                        Text(row.outcome ?? (row.eligible ? "可回收" : "保留"))
                            .font(.caption).foregroundColor(row.eligible ? .green : .secondary)
                    }
                    if !row.detail.isEmpty {
                        Text(row.detail).font(.caption).foregroundColor(.secondary)
                            .lineLimit(2).truncationMode(.middle).help(row.detail).textSelection(.enabled)
                    }
                    Text(row.reason).font(.caption).foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let command = row.command {
                        Button(row.id == "project" ? "复制构建入口" : "复制收尾核对命令") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(command, forType: .string)
                        }.font(.caption)
                    }
                }.padding(.vertical, 9)
                Divider()
            }
        } label: {
            HStack {
                Text(title).font(.headline)
                Text("\(rows.count) 项").font(.caption).foregroundColor(.secondary)
                Spacer()
                if rows.contains(where: { $0.bytes != nil }) {
                    Text(byteCount(rows.reduce(0) { $0 + ($1.bytes ?? 0) })).font(.callout).foregroundColor(.secondary)
                }
            }
        }
    }
}
