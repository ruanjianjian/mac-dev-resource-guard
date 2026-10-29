import AppKit
import SwiftUI

/// The menu-bar panel: one book-like cover, and three quiet pages behind it.
enum Ink {
    static let navy = Color(hex: 0x1D2B53), navyTrack = Color(hex: 0x2E3D6B), navySoft = Color(hex: 0xB9C2DE)
    static let warm = Color(hex: 0xA94A2C), warmTrack = Color(hex: 0xC0613F), warmSoft = Color(hex: 0xF2C9B8)
    static let cream = Color(hex: 0xF3E9C6), coral = Color(hex: 0xF0997B)
    static let paper = Color(hex: 0xFAF8F4), text = Color(hex: 0x2B2A27), muted = Color(hex: 0x8A877F)
    static let rule = Color(hex: 0xE6E2DA), bars = [Color(hex: 0x1D2B53), Color(hex: 0x6B7591), Color(hex: 0xC9C5BC)]
}

extension Color {
    init(hex: UInt32) {
        self.init(red: Double(hex >> 16 & 0xFF) / 255, green: Double(hex >> 8 & 0xFF) / 255, blue: Double(hex & 0xFF) / 255)
    }
}

func number0(_ value: Double?) -> String { value.map { String(format: "%.0f", $0) } ?? "—" }

/// Sizes the way people say them: 27 GB, 4.2 GB, 18 MB — no locale commas, no long decimals.
func size(_ bytes: Int64) -> String {
    let gib = Double(bytes) / 1_073_741_824, mib = Double(bytes) / 1_048_576
    if gib >= 1024 { return String(format: "%.1f TB", gib / 1024) }
    if gib >= 10 { return String(format: "%.0f GB", gib) }
    if gib >= 1 { return String(format: "%.1f GB", gib) }
    return mib >= 1 ? String(format: "%.0f MB", mib) : "不到 1 MB"
}

extension Snapshot {
    /// Free-space level for the menu bar and the cover: 0 enough, 1 below warning, 2 below critical.
    var freeLevel: Int {
        guard let free else { return 0 }
        if let critical, free < critical { return 2 }
        if let warning, free < warning { return 1 }
        return 0
    }
    var usedFraction: Double {
        guard let free, let capacity, capacity > 0 else { return 0 }
        return min(1, max(0, 1 - free / capacity))
    }
}

enum CoverPage { case cover, pending, space, switches, plan, memory }

/// Loads the checkup and the pending list on its own, so the cover never waits for the old panel's work.
final class CoverStore: ObservableObject {
    @Published var page: CoverPage = .cover
    @Published var checkup: BackendReply?
    @Published var pending: BackendReply?
    @Published var plan: BackendReply?
    @Published var memory: BackendReply?
    @Published var working = ""
    @Published var message = ""
    /// The confirmed items that were not deleted, each with its reason; shown until the next action.
    @Published var skipped: [ResourceRow] = []
    /// Ids just deleted, in order: the list ticks them and folds them away one by one, then reloads.
    @Published var cleared: [String] = []
    private var loading = false
    private var again = false

    func refresh(scan: Bool = false) {
        // A refresh asked for while one runs (say, right after a clean) runs again afterwards, never dropped.
        guard !loading else { again = true; return }
        loading = true
        if scan { working = "正在体检，任务多时要几分钟…" }
        DispatchQueue.global(qos: .utility).async {
            let checkup = try? DashboardActions.call(scan ? "checkup-scan" : "checkup-preview")
            let pending = try? DashboardActions.call("pending-preview")
            let plan = try? DashboardActions.call("plan-preview")
            let memory = try? DashboardActions.call("memory-page")
            DispatchQueue.main.async {
                if let checkup, checkup.status == "ok" { self.checkup = checkup }
                if let pending, pending.status == "ok" { self.pending = pending }
                if let plan, plan.status == "ok" { self.plan = plan }
                if let memory, memory.status == "ok" { self.memory = memory }
                self.loading = false
                if scan { self.working = "" }
                if self.again { self.again = false; self.refresh() }
            }
        }
    }

    func perform(_ action: String, ids: [String], token: String? = nil, doing: String, path: String? = nil) {
        guard working.isEmpty else { return }
        working = doing; message = ""
        DispatchQueue.global(qos: .utility).async {
            let reply = try? DashboardActions.call(action, token: token, path: path, ids: ids.isEmpty ? nil : ids)
            DispatchQueue.main.async {
                self.working = ""
                self.message = reply?.message ?? "没连上后台，请稍后再试。"
                if action.hasPrefix("checkup"), let reply, reply.status == "ok" { self.checkup = reply }
                if action == "plan-target", let reply, reply.status == "ok" { self.plan = reply; self.message = "" }
                self.refresh()
                (NSApp.delegate as? AppDelegate)?.model.refresh()
            }
        }
    }

    /// 「删除所选」: re-read the list right now and run the ticked items still on it, so a confirmation never
    /// goes stale; a background cleanup in progress is waited for (up to 2 minutes) instead of refusing.
    func cleanPending(_ ids: [String]) {
        guard working.isEmpty, !ids.isEmpty else { return }
        working = "正在核对并删除 \(ids.count) 项，好了会告诉你…"; message = ""; skipped = []
        let titles = Dictionary(pendingRows.map { ($0.id, $0.title) }, uniquingKeysWith: { a, _ in a })
        DispatchQueue.global(qos: .utility).async {
            var reply: BackendReply?
            var gone: [String] = []
            for attempt in 0..<40 {
                guard let preview = try? DashboardActions.call("pending-preview"), preview.status == "ok" else { break }
                let listed = Set(preview.rows?.filter(\.eligible).map(\.id) ?? [])
                gone = ids.filter { !listed.contains($0) }
                let run = ids.filter(listed.contains)
                if run.isEmpty { break }
                reply = try? DashboardActions.call("pending-clean", token: preview.token, ids: run)
                guard reply?.status == "busy" else { break }
                if attempt == 0 { DispatchQueue.main.async { self.working = "后台正在整理，等它做完就删…" } }
                Thread.sleep(forTimeInterval: 3)
            }
            DispatchQueue.main.async {
                self.working = ""
                let rows = reply?.rows ?? []
                let done = rows.filter { $0.outcome == "已完成" }
                // The backend names an item it no longer lists by its id; show the title the person ticked.
                self.skipped = rows.filter { $0.outcome != "已完成" }.map { row in
                    row.title != row.id ? row : ResourceRow(id: row.id, title: titles[row.id] ?? row.id, detail: row.detail, category: row.category,
                                                           bytes: row.bytes, eligible: false, reason: row.reason, outcome: row.outcome,
                                                           command: nil, kind: row.kind, loses: nil)
                } + gone.map {
                    ResourceRow(id: $0, title: titles[$0] ?? $0, detail: "", category: "执行结果", bytes: nil, eligible: false,
                                reason: "已经处理过或状态变了", outcome: "已跳过", command: nil, kind: nil, loses: nil)
                }
                let freed = done.reduce(Int64(0)) { $0 + ($1.bytes ?? 0) }
                if reply == nil && gone.isEmpty { self.message = "没连上后台，请稍后再试。" }
                else if reply?.status == "busy" { self.message = "后台整理一直没结束，这次没有删除，请稍后再试。" }
                else if let reply, reply.status != "ok" { self.message = reply.message }
                else if done.isEmpty { self.message = "这次没有删掉任何一项，原因见上。" }
                else { self.message = "已删除 \(done.count) 项" + (freed > 0 ? "，腾出约 \(size(freed))" : "") + (self.skipped.isEmpty ? "。" : "；\(self.skipped.count) 项没删，原因见上。") }
                self.cleared = done.map(\.id)
                let fold = Motion.reduced ? 0 : Double(done.count) * 0.15 + 0.6
                DispatchQueue.main.asyncAfter(deadline: .now() + fold) {
                    self.refresh()
                    (NSApp.delegate as? AppDelegate)?.model.refresh()
                }
            }
        }
    }

    func rows(_ category: String) -> [ResourceRow] { checkup?.rows?.filter { $0.category == category } ?? [] }
    func suggestion(_ id: String) -> ResourceRow? { rows("建议").first { $0.id == id } }
    var drive: String? { rows("外接盘").first { $0.outcome == "在用" }?.title }
    /// 「外接盘「T9」未连接」 → T9
    var unplugged: String? {
        guard let title = rows("提醒").first?.title, let a = title.firstIndex(of: "「"), let b = title.firstIndex(of: "」") else { return nil }
        return String(title[title.index(after: a)..<b])
    }
    var pendingRows: [ResourceRow] { pending?.rows?.filter { $0.category == "待确认" } ?? [] }
    func memoryRows(_ category: String) -> [ResourceRow] { memory?.rows?.filter { $0.category == category } ?? [] }
    /// 「模拟器 5 台占 18 GB」 for the cover's memory line; the biggest user otherwise.
    var memorySummary: String {
        let sims = (memory?.rows ?? []).filter { $0.id.hasPrefix("sim:") }
        if !sims.isEmpty { return "模拟器 \(sims.count) 台占 \(size(sims.reduce(0) { $0 + ($1.bytes ?? 0) }))" }
        return memoryRows("正在用").first.map { "主要是 " + $0.title } ?? "点开看是谁在用"
    }
    var target: ResourceRow? { plan?.rows?.first { $0.category == "目标" } }
    var targetGB: Double? { target.flatMap { Double($0.title.filter { $0.isNumber }) } }
    /// Measured against the number on the cover, so the two never disagree.
    func gapGB(_ free: Double?) -> Double { max(0, (targetGB ?? 0) - (free ?? 0)) }
    func planRows(_ category: String) -> [ResourceRow] { plan?.rows?.filter { $0.category == category } ?? [] }

    /// agent-handoff AC-04: the words for the agent go to the clipboard, and the agent App opens.
    func handOff() {
        DispatchQueue.global(qos: .utility).async {
            let reply = try? DashboardActions.call("plan-handoff")
            DispatchQueue.main.async {
                guard let reply else { self.message = "没连上后台，请稍后再试。"; return }
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(reply.message, forType: .string)
                if let app = reply.rows?.first, app.eligible {
                    NSWorkspace.shared.open(URL(fileURLWithPath: app.detail))
                    self.message = "要说的话已复制，在 \(app.title) 里粘贴发送即可。"
                } else {
                    self.message = "要说的话已复制，粘到你的 agent 对话里发送即可。"
                }
            }
        }
    }
}

struct CoverPanel: View {
    @ObservedObject var model: Monitor
    @ObservedObject var store: CoverStore

    var body: some View {
        Group {
            switch store.page {
            case .cover: Cover(model: model, store: store)
            case .pending: PendingPage(model: model, store: store)
            case .space: SpacePage(model: model, store: store)
            case .switches: SwitchesPage(model: model, store: store)
            case .plan: PlanPage(model: model, store: store)
            case .memory: MemoryPage(model: model, store: store)
            }
        }
        .frame(width: 300, height: 340)
    }
}

private struct Cover: View {
    @ObservedObject var model: Monitor
    @ObservedObject var store: CoverStore

    var body: some View {
        let s = model.snapshot
        let tight = s.freeLevel > 0
        let soft = tight ? Ink.warmSoft : Ink.navySoft
        let shift: CGFloat = s.memoryLevel > 0 ? 24 : 0
        ZStack(alignment: .topLeading) {
            (tight ? Ink.warm : Ink.navy)
            VStack(alignment: .leading, spacing: 2) {
                if s.memoryLevel > 0 {
                    Button { store.page = .memory } label: {
                        Text((s.memoryLevel == 2 ? "内存很紧 · " : "内存紧张 · ") + store.memorySummary + " ›")
                            .font(.system(size: 11, weight: .medium)).foregroundColor(Color(hex: 0x7A2E16))
                            .padding(.horizontal, 10).padding(.vertical, 3).background(Capsule().fill(Ink.cream))
                    }.buttonStyle(.plain).padding(.bottom, 6)
                }
                Text("还剩").font(.system(size: 12)).foregroundColor(soft)
                HStack(spacing: 0) {
                    if let free = s.free {
                        RollingNumber(value: free).animation(Motion.reduced ? nil : .easeOut(duration: 0.6), value: free)
                    } else { Text("—") }
                    Text(" GB")
                }.font(.system(size: 22, weight: .medium)).foregroundColor(.white)
                if let target = store.target {
                    // The user's target and how far off it is (space-target-plan AC-03); tap for the plan.
                    let gap = store.gapGB(s.free)
                    Button { store.page = .plan } label: {
                        Text(gap < 1 ? "\(target.title) · 已达到 ›" : "\(target.title) · 还差 \(number0(gap)) GB ›")
                            .font(.system(size: 11)).foregroundColor(Ink.cream)
                    }.buttonStyle(.plain).padding(.top, 2)
                }
            }.padding(18)
            Button { store.page = .space } label: {
                ZStack {
                    Circle().stroke(tight ? Ink.warmTrack : Ink.navyTrack, lineWidth: 30)
                    Circle().trim(from: 0, to: s.usedFraction).stroke(Ink.cream, lineWidth: 30).rotationEffect(.degrees(-90))
                        .animation(Motion.reduced ? nil : .easeOut(duration: 0.8), value: s.usedFraction)
                    if model.work != nil && !Motion.reduced { SweepArc(lineWidth: 30) }
                }.frame(width: 104, height: 104).padding(15).contentShape(Rectangle())
            }.buttonStyle(.plain).help("空间去哪了")
                .frame(maxWidth: .infinity).padding(.top, 92 + shift)
            if let work = model.work {
                WorkLine(work: work, free: s.free, drive: store.drive, colour: Ink.cream)
                    .frame(maxWidth: .infinity).padding(.top, 236 + shift)
            }
            VStack { Spacer(); HStack(alignment: .bottom) {
                Button { store.page = .switches } label: {
                    VStack(alignment: .leading, spacing: 1) { ForEach(Array(leftLines(tight).enumerated()), id: \.offset) { _, line in
                        Text(line.0).foregroundColor(line.1)
                    } }.font(.system(size: 11)).contentShape(Rectangle())
                }.buttonStyle(.plain).help("帮你管着")
                Spacer()
                Button { if store.unplugged == nil && s.pending > 0 { store.page = .pending } else { store.page = .switches } } label: {
                    Text(rightLine(s)).font(.system(size: 12)).foregroundColor(store.unplugged == nil ? Ink.cream : Ink.coral)
                }.buttonStyle(.plain)
            } }.padding(18)
        }
    }

    private func leftLines(_ tight: Bool) -> [(String, Color)] {
        if tight, let freed = store.checkup?.eligibleBytes, freed >= 1 << 30 {
            return [("还能腾出", Ink.cream), ("约 \(size(freed))", Ink.cream)]
        }
        var lines: [(String, Color)] = []
        if store.suggestion("caches")?.outcome == "已开启" { lines.append(("缓存共享", Ink.cream)) }
        if store.suggestion("clean")?.outcome == "已开启" { lines.append(("自动清理", Ink.cream)) }
        if let gone = store.unplugged { lines.append(("\(gone) 未连接", Ink.coral)) }
        else if let drive = store.drive { lines.append(("\(drive) 就绪", Ink.cream)) }
        return lines.isEmpty ? [(store.checkup == nil ? "" : "还没开启", Ink.cream)] : lines
    }

    private func rightLine(_ s: Snapshot) -> String {
        if store.unplugged != nil { return "插上就恢复" }
        return s.pending > 0 ? "\(s.pending) 待确认 ›" : "没有待办"
    }
}

/// Under the ring while the background works: dots flowing to the drive, or the space freed so far.
private struct WorkLine: View {
    let work: Work
    let free: Double?
    let drive: String?
    let colour: Color
    var body: some View {
        VStack(spacing: 3) {
            if work.kind == "move" {
                HStack(spacing: 6) {
                    Text("内置盘")
                    FlowDots(color: colour).frame(width: 46, height: 8)
                    Text(drive ?? "外接盘")
                }
                Text(work.title + (work.bytes > 0 ? " · " + size(work.bytes) : "")).lineLimit(1)
            } else {
                HStack(spacing: 0) {
                    Text("正在整理 · 已腾出 ")
                    RollingNumber(value: work.freed(free), format: "%.1f").animation(Motion.reduced ? nil : .easeOut(duration: 0.6), value: work.freed(free))
                    Text(" GB")
                }
            }
        }.font(.system(size: 11)).foregroundColor(colour).padding(.horizontal, 18)
    }
}

/// Memory: what was handled, what can be closed (one click), and what stays because it is in use.
private struct MemoryPage: View {
    @ObservedObject var model: Monitor
    @ObservedObject var store: CoverStore
    @State private var selected: Set<String> = []

    var body: some View {
        let status = store.memoryRows("状态").first
        let closable = store.memoryRows("可以关掉")
        Page(model: model, store: store,
             title: "内存 · " + (status.map { $0.title + ($0.reason.isEmpty ? "" : "，" + $0.reason) } ?? "正在读取…")) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if let why = store.memoryRows("说明").first {
                        Text(why.reason).font(.system(size: 11)).foregroundColor(Ink.warm).fixedSize(horizontal: false, vertical: true)
                    }
                    MemorySection(title: "自动处理了", rows: store.memoryRows("自动处理了"))
                    if !closable.isEmpty {
                        SectionTitle(text: "可以关掉（没被开发工具连着）")
                        ForEach(closable) { row in
                            Button { toggle(row.id) } label: { MemoryLine(row: row, on: selected.contains(row.id)) }.buttonStyle(.plain)
                        }
                        Text("两小时没连过的会在内存紧张时自动关，数据都在。")
                            .font(.system(size: 10.5)).foregroundColor(Ink.muted).padding(.top, 6)
                    }
                    MemorySection(title: "正在用，不能动", rows: store.memoryRows("正在用"))
                }
            }
            if !closable.isEmpty {
                HStack {
                    Text(selected.isEmpty ? "" : "已选 \(selected.count) 台").foregroundColor(Ink.muted)
                    Spacer()
                    Button("关掉所选") {
                        store.perform("memory-close", ids: Array(selected), doing: "正在关…")
                        selected = []
                    }.buttonStyle(.plain).font(.system(size: 12, weight: .medium))
                        .foregroundColor(selected.isEmpty ? Ink.muted : Ink.navy).disabled(selected.isEmpty || !store.working.isEmpty)
                }.font(.system(size: 12)).padding(.top, 10)
            }
        }
        .onAppear { store.refresh() }
    }
    private func toggle(_ id: String) { if selected.contains(id) { selected.remove(id) } else { selected.insert(id) } }
}

private struct SectionTitle: View {
    let text: String
    var body: some View {
        Text(text).font(.system(size: 11)).foregroundColor(Ink.muted).padding(.top, 10).padding(.bottom, 2)
    }
}

private struct MemorySection: View {
    let title: String
    let rows: [ResourceRow]
    var body: some View {
        if !rows.isEmpty {
            SectionTitle(text: title)
            ForEach(rows) { row in MemoryLine(row: row, on: nil) }
        }
    }
}

/// One memory row: name, why, size; with a box only when it can be closed.
private struct MemoryLine: View {
    let row: ResourceRow
    let on: Bool?
    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            if let on { Checkbox(on: on).padding(.top, 2) }
            VStack(alignment: .leading, spacing: 2) {
                Text(row.title).font(.system(size: 12)).foregroundColor(Ink.text).lineLimit(1)
                if let tag = row.tag, !row.reason.isEmpty { StatusTag(kind: tag, text: row.reason).padding(.top, 2) }
                else if !row.reason.isEmpty { Text(row.reason).font(.system(size: 11)).foregroundColor(Ink.muted).lineLimit(1) }
            }
            Spacer(minLength: 6)
            if let bytes = row.bytes, bytes > 0 { Text(size(bytes)).font(.system(size: 11)).foregroundColor(Ink.muted) }
        }
        .padding(.vertical, 5).contentShape(Rectangle())
        .overlay(Rectangle().fill(Ink.rule).frame(height: 0.5), alignment: .bottom)
    }
}

/// A bright pill for a memory row's state, one colour each, so a long list reads at a glance:
/// green can be closed, blue in use, violet a dev tool is attached, amber idle and closed automatically.
private struct StatusTag: View {
    let kind: String
    let text: String
    static let colours: [String: (fill: UInt32, ink: UInt32, dot: UInt32)] = [
        "free": (0xDCF4E4, 0x16703C, 0x22A35A), "use": (0xE3EBFF, 0x2747A8, 0x3A63D6),
        "link": (0xEEE5FF, 0x5534B8, 0x7650E0), "auto": (0xFFEED4, 0x9A5600, 0xE08A10)]

    var body: some View {
        let colour = StatusTag.colours[kind] ?? StatusTag.colours["use"]!
        HStack(spacing: 4) {
            Circle().fill(Color(hex: colour.dot)).frame(width: 5, height: 5)
            Text(text).font(.system(size: 10.5, weight: .medium)).foregroundColor(Color(hex: colour.ink)).lineLimit(1)
        }
        .padding(.leading, 6).padding(.trailing, 7).padding(.vertical, 3)
        .background(Capsule().fill(Color(hex: colour.fill)))
    }
}

/// The quiet page layout shared by the three pages behind the cover.
private struct Page<Content: View>: View {
    @ObservedObject var model: Monitor
    @ObservedObject var store: CoverStore
    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { store.page = .cover; store.message = ""; store.skipped = [] } label: {
                Text("‹ 还剩 \(number0(model.snapshot.free)) GB").font(.system(size: 12)).foregroundColor(Ink.muted)
            }.buttonStyle(.plain)
            Text(title).font(.system(size: 16, weight: .medium)).foregroundColor(Ink.navy).padding(.top, 4).padding(.bottom, 10)
            content
            if !store.working.isEmpty {
                HStack(spacing: 6) { ProgressView().controlSize(.small); Text(store.working) }
                    .font(.system(size: 11)).foregroundColor(Ink.muted).padding(.top, 8)
            } else if !store.message.isEmpty {
                Text(store.message).font(.system(size: 11)).foregroundColor(Ink.muted)
                    .fixedSize(horizontal: false, vertical: true).padding(.top, 8)
            }
        }
        .padding(.horizontal, 18).padding(.vertical, 16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Ink.paper)
    }
}

private struct PendingPage: View {
    @ObservedObject var model: Monitor
    @ObservedObject var store: CoverStore
    @State private var selected: Set<String> = []
    @State private var confirming = false
    @State private var ticked: Set<String> = []
    @State private var gone: Set<String> = []

    var body: some View {
        let rows = store.pendingRows
        let busy = !store.working.isEmpty   // while deleting, nothing here can be tapped
        Page(model: model, store: store, title: "等你确认") {
            if rows.isEmpty {
                ForEach(store.skipped) { row in SkippedLine(row: row) }
                Text(store.pending == nil ? "正在读取…" : store.skipped.isEmpty ? "没有要你确认的。" : "没有别的要你确认了。")
                    .font(.system(size: 12)).foregroundColor(Ink.muted).padding(.top, store.skipped.isEmpty ? 0 : 10)
                Spacer()
            } else {
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(store.skipped) { row in SkippedLine(row: row) }
                        ForEach(rows.filter { !gone.contains($0.id) }) { row in
                            Button { toggle(row) } label: { PendingLine(row: row, on: selected.contains(row.id), done: ticked.contains(row.id)) }
                                .buttonStyle(.plain).disabled(!row.eligible)
                                .transition(.asymmetric(insertion: .identity, removal: .opacity.combined(with: .move(edge: .leading))))
                                .contextMenu { Button("不再提醒") { store.perform("pending-ignore", ids: [row.id], doing: "正在忽略…") } }
                        }
                    }
                }
                .opacity(busy ? 0.45 : 1).allowsHitTesting(!busy)
                if !busy { HStack(spacing: 8) {
                    // One tap selects everything that can run now; tap again to clear.
                    Button { selected = allSelected ? [] : Set(selectable) } label: {
                        HStack(spacing: 6) {
                            Checkbox(on: allSelected, partly: !selected.isEmpty && !allSelected)
                            Text("全选").foregroundColor(Ink.text)
                        }.contentShape(Rectangle())
                    }.buttonStyle(.plain).disabled(selectable.isEmpty)
                    Text(selected.isEmpty ? "" : "已选 \(size(chosen.reduce(0) { $0 + ($1.bytes ?? 0) }))")
                        .foregroundColor(Ink.muted)
                    Spacer()
                    Button("删除所选") { confirming = true }.buttonStyle(.plain)
                        .foregroundColor(selected.isEmpty ? Ink.muted : Ink.navy).font(.system(size: 12, weight: .medium))
                        .disabled(selected.isEmpty)
                }.font(.system(size: 12)).padding(.top, 10) }
            }
        }
        .onAppear { selected = Set(initialSelection); store.refresh() }
        .onChange(of: store.cleared) { ids in fold(ids) }
        .alert("删除所选 \(selected.count) 项？", isPresented: $confirming) {
            Button("取消", role: .cancel) {}
            Button("删除", role: .destructive) {
                store.cleanPending(Array(selected))
                selected = []
            }
        } message: {
            Text("会失去：" + Set(chosen.compactMap(\.loses)).sorted().joined(separator: "；") + "。执行前会逐项再核对。")
        }
    }

    /// Offscreen evidence can show a selection without clicking.
    private var initialSelection: [String] {
        (ProcessInfo.processInfo.environment["RENDER_SELECT"] ?? "").split(separator: ",").map(String.init)
    }
    private var chosen: [ResourceRow] { store.pendingRows.filter { selected.contains($0.id) } }
    private var selectable: [String] { store.pendingRows.filter(\.eligible).map(\.id) }
    private var allSelected: Bool { !selectable.isEmpty && Set(selectable).isSubset(of: selected) }
    private func fold(_ ids: [String]) {
        // Top to bottom, the order the rows are on screen.
        for (index, id) in store.pendingRows.map(\.id).filter(ids.contains).enumerated() {
            let start = Motion.reduced ? 0 : Double(index) * 0.15
            DispatchQueue.main.asyncAfter(deadline: .now() + start) {
                withAnimation(Motion.reduced ? nil : .easeOut(duration: 0.15)) { _ = ticked.insert(id) }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + start + (Motion.reduced ? 0 : 0.35)) {
                withAnimation(Motion.reduced ? nil : .easeOut(duration: 0.25)) { _ = gone.insert(id) }
            }
        }
    }
    private func toggle(_ row: ResourceRow) {
        if selected.contains(row.id) { selected.remove(row.id) } else { selected.insert(row.id) }
    }
}

private struct Checkbox: View {
    let on: Bool
    var partly = false
    var body: some View {
        RoundedRectangle(cornerRadius: 3).fill(on ? Ink.navy : Color.clear)
            .overlay(RoundedRectangle(cornerRadius: 3).stroke(on || partly ? Ink.navy : Color(hex: 0xB4B2A9), lineWidth: 1))
            .overlay(Rectangle().fill(Ink.navy).frame(width: 7, height: 1.5).opacity(partly ? 1 : 0))
            .frame(width: 13, height: 13)
    }
}

/// A confirmed item that was not deleted, and why.
private struct SkippedLine: View {
    let row: ResourceRow
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("没删 · " + (row.title.split(separator: "：", maxSplits: 1).last.map(String.init) ?? row.title))
                .font(.system(size: 12)).foregroundColor(Ink.warm).lineLimit(1)
            Text(row.reason).font(.system(size: 11)).foregroundColor(Ink.muted).lineLimit(2)
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 8)
        .overlay(Rectangle().fill(Ink.rule).frame(height: 0.5), alignment: .bottom)
    }
}

private struct PendingLine: View {
    let row: ResourceRow
    let on: Bool
    var done = false
    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Group {
                if done { Image(systemName: "checkmark").font(.system(size: 10, weight: .semibold)).foregroundColor(Ink.navy).frame(width: 13, height: 13) }
                else { Checkbox(on: on) }
            }.padding(.top, 2)
            VStack(alignment: .leading, spacing: 2) {
                Text(row.title.split(separator: "：", maxSplits: 1).last.map(String.init) ?? row.title)
                    .font(.system(size: 12)).foregroundColor(Ink.text).lineLimit(1)
                Text(row.outcome == "外接盘未连接" ? "外接盘未连接" : row.reason).font(.system(size: 11)).foregroundColor(Ink.muted).lineLimit(1)
            }
            Spacer(minLength: 6)
            if let bytes = row.bytes, bytes > 0 { Text(size(bytes)).font(.system(size: 11)).foregroundColor(Ink.muted) }
        }
        .padding(.vertical, 8).contentShape(Rectangle())
        .overlay(Rectangle().fill(Ink.rule).frame(height: 0.5), alignment: .bottom)
    }
}

private struct SpacePage: View {
    @ObservedObject var model: Monitor
    @ObservedObject var store: CoverStore
    var body: some View {
        let rows = store.rows("空间去哪了").filter { ($0.bytes ?? 0) > 0 || $0.outcome == "外接盘" }
        let largest = Double(max(1, rows.compactMap(\.bytes).max() ?? 1))
        Page(model: model, store: store, title: "空间去哪了") {
            ScrollView {
                VStack(alignment: .leading, spacing: 13) {
                    ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
                        VStack(alignment: .leading, spacing: 5) {
                            HStack {
                                Text(row.title).foregroundColor(Ink.text)
                                Spacer()
                                Text(row.bytes.map(size) ?? "在外接盘").foregroundColor(Ink.muted)
                            }.font(.system(size: 12))
                            GeometryReader { g in
                                Rectangle().fill(Ink.bars[min(index, 2)])
                                    .frame(width: max(2, g.size.width * CGFloat(Double(row.bytes ?? 0) / largest)), height: 3)
                            }.frame(height: 3)
                        }
                    }
                }
            }
            Button("重新体检") { store.refresh(scan: true) }.buttonStyle(.plain)
                .font(.system(size: 12)).foregroundColor(Ink.navy).padding(.top, 8)
                .disabled(!store.working.isEmpty)
        }
    }
}

private struct PlanPage: View {
    @ObservedObject var model: Monitor
    @ObservedObject var store: CoverStore
    var body: some View {
        Page(model: model, store: store, title: "计划") {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if let target = store.target {
                        let gap = store.gapGB(model.snapshot.free)
                        Text(gap < 1 ? "\(target.title)，已达到。" : "\(target.title)，还差 \(number0(gap)) GB。")
                            .font(.system(size: 12)).foregroundColor(Ink.text)
                    }
                    ForEach(store.planRows("计划")) { section in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text(section.title).font(.system(size: 12, weight: .medium)).foregroundColor(Ink.navy)
                                Spacer()
                                Text((section.bytes ?? 0) > 0 ? size(section.bytes ?? 0) : "—").font(.system(size: 12)).foregroundColor(Ink.muted)
                            }
                            let items = store.planRows(section.id)
                            if items.isEmpty {
                                Text(section.id == "auto" ? "暂时没有，后台每 30 分钟看一次" : "没有").font(.system(size: 11)).foregroundColor(Ink.muted)
                            }
                            ForEach(items.prefix(2)) { entry in
                                HStack(alignment: .top) {
                                    Text(entry.title).lineLimit(1)
                                    Spacer(minLength: 6)
                                    if let bytes = entry.bytes { Text(size(bytes)) }
                                }.font(.system(size: 11)).foregroundColor(Ink.muted)
                            }
                        }
                        .padding(.bottom, 4)
                        .overlay(Rectangle().fill(Ink.rule).frame(height: 0.5), alignment: .bottom)
                    }
                    if let reach = store.plan?.message, !reach.isEmpty {
                        Text(reach + "。").font(.system(size: 12)).foregroundColor(Ink.text)
                    }
                    ForEach(store.planRows("建议")) { tip in
                        Text("· " + tip.title + "：" + tip.reason).font(.system(size: 11)).foregroundColor(Ink.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            HStack {
                Button("交给 agent") { store.handOff() }.buttonStyle(.plain)
                    .font(.system(size: 12, weight: .medium)).foregroundColor(Ink.navy)
                Spacer()
                if store.pendingRows.count > 0 {
                    Button("去确认 ›") { store.page = .pending }.buttonStyle(.plain)
                        .font(.system(size: 12)).foregroundColor(Ink.muted)
                }
            }.padding(.top, 8)
        }
    }
}

private struct SwitchesPage: View {
    @ObservedObject var model: Monitor
    @ObservedObject var store: CoverStore
    @State private var confirmingRestore = false
    private func setTarget(_ gb: Double) {
        store.perform("plan-target", ids: [], token: nil, doing: "正在改目标…", path: String(Int(gb)))
    }
    private func flip(_ id: String, on: Bool) {
        if !on { store.perform("checkup-apply", ids: [id], doing: "正在开启…") }
        else if id == "caches" { confirmingRestore = true }
        else { store.perform("checkup-off", ids: [id], doing: "正在关闭…") }
    }
    private func title(_ id: String) -> String {
        switch id {
        case "caches": return "共享构建缓存"
        case "clean": return "自动清理"
        default: return store.drive.map { "外接盘 \($0)" } ?? "外接盘"
        }
    }

    var body: some View {
        Page(model: model, store: store, title: "帮你管着") {
            VStack(spacing: 0) {
                // The one number the user sets (space-target-plan AC-01).
                if let gb = store.targetGB {
                    SettingRow(title: "空间目标", detail: "内置盘至少留这么多") {
                        HStack(spacing: 6) {
                            StepButton(symbol: "minus", label: "目标减 10 GB") { setTarget(max(10, gb - 10)) }
                            Text("\(Int(gb)) GB").font(.system(size: 12)).foregroundColor(Ink.navy).monospacedDigit()
                            StepButton(symbol: "plus", label: "目标加 10 GB") { setTarget(gb + 10) }
                        }.disabled(!store.working.isEmpty)
                    }
                }
                ForEach(["caches", "clean", "external"], id: \.self) { id in
                    let row = store.suggestion(id)
                    let on = row?.outcome == "已开启"
                    SettingRow(title: title(id), detail: row?.detail ?? "") {
                        Switch(on: on, label: title(id)) { flip(id, on: on) }
                            .disabled(row == nil || (row?.eligible == false && !on) || !store.working.isEmpty)
                    }
                }
            }
            Spacer()
            if let last = store.rows("最近").first {
                Text(last.title).font(.system(size: 11)).foregroundColor(Ink.muted)
            }
        }
        .alert("还原构建缓存设置？", isPresented: $confirmingRestore) {
            Button("取消", role: .cancel) {}
            Button("还原") { store.perform("checkup-off", ids: ["caches"], doing: "正在还原…") }
        } message: {
            Text("看板做的链接会去掉，各工具从空开始重建缓存；外接盘上的缓存副本会删除；Xcode 编译缓存关闭。任务的构建产物留在外接盘，你自己做的链接不动。")
        }
    }
}

/// One setting: name and a short line on the left, its control on the shared right edge.
private struct SettingRow<Control: View>: View {
    let title: String
    let detail: String
    @ViewBuilder var control: Control
    var body: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 12)).foregroundColor(Ink.text)
                Text(detail).font(.system(size: 11)).foregroundColor(Ink.muted).lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            control
        }
        .padding(.vertical, 9)
        .overlay(Rectangle().fill(Ink.rule).frame(height: 0.5), alignment: .bottom)
    }
}

/// The cover's own switch: navy when on, the knob slides.
private struct Switch: View {
    let on: Bool
    let label: String
    let action: () -> Void
    @Environment(\.isEnabled) private var enabled
    var body: some View {
        Button(action: action) {
            Capsule().fill(on ? Ink.navy : Color(hex: 0xD3D1C7))
                .overlay(Circle().fill(Ink.paper).padding(2).offset(x: on ? 6 : -6))
                .frame(width: 30, height: 18)
                .animation(Motion.reduced ? nil : .easeOut(duration: 0.15), value: on)
        }
        .buttonStyle(.plain).opacity(enabled ? 1 : 0.45)
        .accessibilityLabel(label).accessibilityValue(on ? "已开启" : "未开启")
    }
}

private struct StepButton: View {
    let symbol: String
    let label: String
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 9, weight: .medium)).foregroundColor(Ink.muted)
                .frame(width: 16, height: 16).contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityLabel(label)
    }
}
