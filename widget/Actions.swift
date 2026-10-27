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
    @Published var busy = false
    @Published var activity = ""
    @Published var message = ""
    @Published var lastStatus = ""
    @Published var previews: [String: BackendReply] = [:]

    func run(_ family: String, clean: Bool = false, action: String? = nil, path: String? = nil) {
        guard !busy, ["storage", "common", "memory", "setup", "task"].contains(family) else { return }
        let preview = previews[family]
        if clean && preview?.canClean != true {
            message = "请先重新扫描，查看本次预览。"; return
        }
        let token = clean ? preview?.token : nil
        previews[family] = nil  // Neither failed requests nor old results enable cleaning.
        busy = true; message = ""
        activity = clean ? "后端正在重新核对保护并执行，请等待完成…" :
            (["storage", "common"].contains(family) ? "正在扫描缓存及占用；大目录可能需要几分钟…" : "正在检查，请稍候…")
        DispatchQueue.global(qos: .utility).async {
            let reply: BackendReply
            do {
                guard let script = Bundle.main.url(forResource: "backend", withExtension: "py") else {
                    throw NSError(domain: "Dashboard", code: 1, userInfo: [NSLocalizedDescriptionKey: "应用缺少后端适配器，请重新安装。"])
                }
                let task = Process()
                task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
                var args = [script.path, action ?? (family + (clean ? "-clean" : "-preview"))]
                if let token { args += ["--token", token] }
                if let path { args += ["--path", path] }
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
                    throw NSError(domain: "Dashboard", code: 3, userInfo: [NSLocalizedDescriptionKey: clean ? "后端连接中断，操作结果待核实；不会自动重试。" : "后端连接失败，请检查工具安装。"])
                }
                reply = try JSONDecoder().decode(BackendReply.self, from: data)
            } catch {
                reply = BackendReply(status: "error", message: error.localizedDescription,
                                     rows: nil, token: nil, created: nil, eligible: nil, eligibleBytes: nil)
            }
            DispatchQueue.main.async {
                self.previews[family] = reply
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
                Text("接入设置").tag("setup")
            }.pickerStyle(.segmented).padding(18)
            if tab == "overview" {
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("先看占用，再决定清理").font(.headline)
                        Text("已有 Xcode 缓存可直接扫描；自动维护需在接入设置单独开启。")
                            .font(.callout).foregroundColor(.secondary)
                        HStack {
                            Button("查看已有缓存") { tab = "storage" }
                            Button("检查接入与自动维护") { tab = "setup" }
                        }
                    }.padding(.horizontal, 24)
                    Overview(model: model).frame(maxWidth: .infinity)
                }
            } else {
                if tab == "setup" { setupControls } else { controls }
                Divider()
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if let reply = preview, let rows = reply.rows, !rows.isEmpty {
                            ForEach(Array(Set(rows.map(\.category))).sorted(), id: \.self) { category in
                                ResourceGroup(title: category, rows: rows.filter { $0.category == category })
                            }
                        } else if !actions.busy {
                            VStack(alignment: .leading, spacing: 10) {
                                Text(preview == nil ? "先查看，再决定" : "本次没有可展示的项目")
                                    .font(.title3.weight(.medium))
                                Text(tab == "storage" ? "首次使用请选择已有缓存并扫描，无需改构建脚本。可清理项、大项占用与保留原因会分别显示。" : "诊断后台归属与闲置证据，正在使用或无法核实的资源保留。")
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
            if value == "setup" && !actions.busy { actions.run("setup") }
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
            Text(setupAction == "setup-upgrade"
                 ? "只升级可识别的本工具旧版本，先备份并校验再切换，保留配置、暂停状态和保护记录。运行中的维护、托管后台、本地修改或其他工具会阻止升级；不会停止它们。"
                 : setupAction == "setup-install"
                 ? "安装内置 dev-storage / dev-memory 命令。已有不同实现会保留并提示冲突，不覆盖状态或保护记录。安装本身不会开启自动清理。"
                 : "\(setupAction.contains("enable") ? "开启" : "关闭")\(setupAction.contains("memory") ? "每分钟观察、满足安全条件才回收的内存维护" : "每 12 小时仅处理托管槽的过期中间缓存维护")。未接入占用协议的模拟器、MCP 仍不会回收；已有缓存与任务目录不进入自动删除范围。")
        }
    }

    private var setupControls: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("先看可用能力，再开启维护").font(.headline)
            Text("已有缓存清理已经内置。自动维护是可选项；仅装好看板不代表后台正在清理。")
                .font(.callout).foregroundColor(.secondary)
            HStack {
                Button("检查接入") { actions.run("setup") }
                Button("安装命令工具") { configure("setup-install") }
                Button("升级本工具") { configure("setup-upgrade") }
            }
            HStack {
                Text("内存自动回收"); Spacer()
                Button("开启") { configure("setup-memory-enable") }
                Button("关闭") { configure("setup-memory-disable") }
            }
            HStack {
                Text("托管槽定期清理"); Spacer()
                Button("开启") { configure("setup-storage-enable") }
                Button("关闭") { configure("setup-storage-disable") }
            }
        }.disabled(actions.busy).padding(.horizontal, 20).padding(.bottom, 16)
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
                        Button("复制收尾核对命令") {
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
