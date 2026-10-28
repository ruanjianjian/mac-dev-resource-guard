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

enum CoverPage { case cover, pending, space, switches }

/// Loads the checkup and the pending list on its own, so the cover never waits for the old panel's work.
final class CoverStore: ObservableObject {
    @Published var page: CoverPage = .cover
    @Published var checkup: BackendReply?
    @Published var pending: BackendReply?
    @Published var working = ""
    @Published var message = ""
    private var loading = false

    func refresh(scan: Bool = false) {
        guard !loading else { return }
        loading = true
        if scan { working = "正在体检，任务多时要几分钟…" }
        DispatchQueue.global(qos: .utility).async {
            let checkup = try? DashboardActions.call(scan ? "checkup-scan" : "checkup-preview")
            let pending = try? DashboardActions.call("pending-preview")
            DispatchQueue.main.async {
                if let checkup, checkup.status == "ok" { self.checkup = checkup }
                if let pending, pending.status == "ok" { self.pending = pending }
                self.loading = false
                if scan { self.working = "" }
            }
        }
    }

    func perform(_ action: String, ids: [String], token: String? = nil, doing: String) {
        guard working.isEmpty else { return }
        working = doing; message = ""
        DispatchQueue.global(qos: .utility).async {
            let reply = try? DashboardActions.call(action, token: token, ids: ids)
            DispatchQueue.main.async {
                self.working = ""
                self.message = reply?.message ?? "没连上后台，请稍后再试。"
                if action.hasPrefix("checkup"), let reply, reply.status == "ok" { self.checkup = reply }
                self.refresh()
                (NSApp.delegate as? AppDelegate)?.model.refresh()
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
        ZStack(alignment: .topLeading) {
            (tight ? Ink.warm : Ink.navy)
            VStack(alignment: .leading, spacing: 2) {
                Text("还剩").font(.system(size: 12)).foregroundColor(soft)
                Text("\(number0(s.free)) GB").font(.system(size: 22, weight: .medium)).foregroundColor(.white)
            }.padding(18)
            Button { store.page = .space } label: {
                ZStack {
                    Circle().stroke(tight ? Ink.warmTrack : Ink.navyTrack, lineWidth: 30)
                    Circle().trim(from: 0, to: s.usedFraction).stroke(Ink.cream, lineWidth: 30).rotationEffect(.degrees(-90))
                }.frame(width: 104, height: 104).padding(15).contentShape(Rectangle())
            }.buttonStyle(.plain).help("空间去哪了")
                .frame(maxWidth: .infinity).padding(.top, 92)
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

/// The quiet page layout shared by the three pages behind the cover.
private struct Page<Content: View>: View {
    @ObservedObject var model: Monitor
    @ObservedObject var store: CoverStore
    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { store.page = .cover; store.message = "" } label: {
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

    var body: some View {
        let rows = store.pendingRows
        Page(model: model, store: store, title: "等你确认") {
            if rows.isEmpty {
                Text(store.pending == nil ? "正在读取…" : "没有要你确认的。").font(.system(size: 12)).foregroundColor(Ink.muted)
                Spacer()
            } else {
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(rows) { row in
                            Button { toggle(row) } label: { PendingLine(row: row, on: selected.contains(row.id)) }
                                .buttonStyle(.plain).disabled(!row.eligible)
                                .contextMenu { Button("不再提醒") { store.perform("pending-ignore", ids: [row.id], doing: "正在忽略…") } }
                        }
                    }
                }
                HStack(spacing: 8) {
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
                        .disabled(selected.isEmpty || !store.working.isEmpty)
                }.font(.system(size: 12)).padding(.top, 10)
            }
        }
        .onAppear { selected = Set(initialSelection); store.refresh() }
        .alert("删除所选 \(selected.count) 项？", isPresented: $confirming) {
            Button("取消", role: .cancel) {}
            Button("删除", role: .destructive) {
                store.perform("pending-clean", ids: Array(selected), token: store.pending?.token, doing: "正在逐项核对并删除…")
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

private struct PendingLine: View {
    let row: ResourceRow
    let on: Bool
    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Checkbox(on: on).padding(.top, 2)
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

private struct SwitchesPage: View {
    @ObservedObject var model: Monitor
    @ObservedObject var store: CoverStore
    @State private var confirmingRestore = false
    private let names = ["caches": "共享构建缓存", "clean": "自动清理", "external": "外接盘"]

    var body: some View {
        Page(model: model, store: store, title: "帮你管着") {
            VStack(spacing: 0) {
                ForEach(["caches", "clean", "external"], id: \.self) { id in
                    let row = store.suggestion(id)
                    HStack(alignment: .top) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(id == "external" ? (store.drive.map { "外接盘 \($0)" } ?? "外接盘") : names[id]!)
                                .font(.system(size: 12)).foregroundColor(Ink.text)
                            Text(row.map { $0.detail.isEmpty ? $0.reason : $0.detail } ?? "").font(.system(size: 11)).foregroundColor(Ink.muted)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 10)
                        Toggle("", isOn: Binding(get: { row?.outcome == "已开启" }, set: { on in
                            if on { store.perform("checkup-apply", ids: [id], doing: "正在开启…") }
                            else if id == "caches" { confirmingRestore = true }
                            else { store.perform("checkup-off", ids: [id], doing: "正在关闭…") }
                        })).toggleStyle(.switch).controlSize(.mini).labelsHidden().tint(Ink.navy)
                            .disabled(row == nil || (row?.eligible == false && row?.outcome != "已开启") || !store.working.isEmpty)
                    }
                    .padding(.vertical, 9)
                    .overlay(Rectangle().fill(Ink.rule).frame(height: 0.5), alignment: .bottom)
                }
            }
            Spacer()
            let off = ["caches", "clean", "external"].filter { store.suggestion($0)?.eligible == true && store.suggestion($0)?.outcome != "已开启" }
            if !off.isEmpty {
                Button("全部开启") { store.perform("checkup-apply", ids: off, doing: "正在开启…") }.buttonStyle(.plain)
                    .font(.system(size: 12, weight: .medium)).foregroundColor(Ink.navy).disabled(!store.working.isEmpty)
            }
        }
        .alert("还原构建缓存设置？", isPresented: $confirmingRestore) {
            Button("取消", role: .cancel) {}
            Button("还原") { store.perform("checkup-off", ids: ["caches"], doing: "正在还原…") }
        } message: {
            Text("看板做的链接会去掉，各工具从空开始重建缓存；外接盘上的缓存副本会删除；Xcode 编译缓存关闭。你自己做的链接不动。")
        }
    }
}
