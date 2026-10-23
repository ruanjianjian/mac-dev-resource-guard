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

final class DashboardActions: ObservableObject {
    @Published var busy = false
    @Published var activity = ""
    @Published var message = ""
    @Published var lastStatus = ""
    @Published var previews: [String: BackendReply] = [:]

    func run(_ family: String, clean: Bool = false) {
        guard !busy, ["storage", "memory"].contains(family) else { return }
        let preview = previews[family]
        if clean && preview?.canClean != true {
            message = "请先重新扫描，查看本次预览。"; return
        }
        let token = clean ? preview?.token : nil
        previews[family] = nil  // Neither failed requests nor old results enable cleaning.
        busy = true; message = ""
        activity = clean ? "后端正在重新核对保护并执行，请等待完成…" :
            (family == "storage" ? "正在扫描受管缓存；大目录可能需要几分钟…" : "正在诊断开发后台…")
        DispatchQueue.global(qos: .utility).async {
            let reply: BackendReply
            do {
                guard let script = Bundle.main.url(forResource: "backend", withExtension: "py") else {
                    throw NSError(domain: "Dashboard", code: 1, userInfo: [NSLocalizedDescriptionKey: "应用缺少后端适配器，请重新安装。"])
                }
                let task = Process()
                task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
                var args = [script.path, family + (clean ? "-clean" : "-preview")]
                if let token { args += ["--token", token] }
                // An explicit, marked disposable home is only for UI integration tests.
                if let index = CommandLine.arguments.firstIndex(of: "--test-home"), index + 1 < CommandLine.arguments.count {
                    let home = CommandLine.arguments[index + 1]
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

    private var preview: BackendReply? { actions.previews[tab] }

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
            }.pickerStyle(.segmented).padding(18)
            if tab == "overview" {
                ScrollView { Overview(model: model).frame(maxWidth: .infinity) }
            } else {
                controls
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
                                Text(tab == "storage" ? "扫描现有工具管理的缓存，查看大小与保留原因。" : "诊断后台归属与闲置证据，正在使用或无法核实的资源保留。")
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
        .alert(tab == "storage" ? "按当前规则清理缓存？" : "安全回收闲置开发后台？", isPresented: $confirming) {
            Button("取消", role: .cancel) {}
            Button("确认清理") { actions.run(tab, clean: true) }
        } message: {
            Text(tab == "storage"
                 ? "预览有 \(preview?.eligible ?? 0) 项符合条件，目录统计约 \(byteCount(preview?.eligibleBytes ?? 0))。后端将按同一规则重新扫描并复核，实际项目以复核为准。仅处理四类中间缓存，保留验收包、报告和设备数据。"
                 : "预览有 \(preview?.eligible ?? 0) 项符合闲置条件。后端仍会复核身份、占用、冷却与待核实请求；仅通过复核的后台正常退出。不会恢复已暂停的守护。")
        }
    }

    private var controls: some View {
        VStack(alignment: .leading, spacing: 12) {
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
                Button(tab == "storage" ? "扫描缓存" : "诊断后台") { actions.run(tab) }
                    .disabled(actions.busy)
                Button(tab == "storage" ? "清理规则内缓存" : "安全回收") { confirming = true }
                    .buttonStyle(.borderedProminent)
                    .disabled(actions.busy || preview?.canClean != true)
            }
            Text(tab == "storage"
                 ? "只处理编译中间文件、索引、模块与 SDK 状态缓存；范围沿用已安装工具的配置。"
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
