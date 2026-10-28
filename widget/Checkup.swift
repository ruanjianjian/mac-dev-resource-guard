import AppKit
import SwiftUI

/// The first tab (checkup-home): where the space went and three suggestions, each one switch.
struct CheckupPage: View {
    @ObservedObject var model: Monitor
    @ObservedObject var actions: DashboardActions
    @State private var confirmingRestore = false

    private var reply: BackendReply? { actions.previews["checkup"] }
    private func rows(_ category: String) -> [ResourceRow] { reply?.rows?.filter { $0.category == category } ?? [] }
    private var suggestions: [ResourceRow] { rows("建议") }
    private var pendingOn: [String] { suggestions.filter { $0.eligible && $0.outcome != "已开启" }.map(\.id) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header
                ForEach(rows("提醒")) { row in
                    VStack(alignment: .leading, spacing: 4) {
                        Label(row.title, systemImage: "externaldrive.badge.exclamationmark").font(.headline).foregroundColor(.orange)
                        Text(row.reason).font(.callout).fixedSize(horizontal: false, vertical: true)
                    }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.12)))
                }
                if reply == nil && !actions.busy {
                    Text("体检会看看空间都去了哪、哪些能省，不会删任何东西。").foregroundColor(.secondary)
                    Button("开始体检") { actions.run("checkup", action: "checkup-scan") }.buttonStyle(.borderedProminent)
                } else if reply?.status == "ok" {
                    suggestionSection
                    found
                    spaceSection
                }
            }.padding(.horizontal, 24).padding(.vertical, 8).frame(maxWidth: .infinity, alignment: .leading)
        }
        .disabled(actions.busy)
        .alert("还原构建缓存设置？", isPresented: $confirmingRestore) {
            Button("取消", role: .cancel) {}
            Button("还原") { actions.run("checkup", action: "checkup-off", ids: ["caches"]) }
        } message: {
            Text("去掉看板做的链接，各工具回到原来的位置，缓存从空开始重新下载或生成；删掉外接盘上的缓存副本；关闭 Xcode 编译缓存。你自己做的链接和改过的位置不动。")
        }
    }

    private var header: some View {
        let s = model.snapshot
        let low = (s.free ?? 0) < (s.warning ?? 0)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text("内置盘还剩").foregroundColor(.secondary)
                Text(number0(s.free)).font(.system(size: 34, weight: .light, design: .rounded)).monospacedDigit()
                    .foregroundColor(s.tintForFree)
                Text("GB / \(number0(s.capacity)) GB").foregroundColor(.secondary)
                Spacer()
                Button("重新体检") { actions.run("checkup", action: "checkup-scan") }
            }
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.08))
                    Capsule().fill(s.tintForFree.opacity(0.85))
                        .frame(width: g.size.width * CGFloat(min(1, max(0, (s.free ?? 0) / max(1, s.capacity ?? 1)))))
                }
            }.frame(height: 6)
            if let message = reply?.message, !message.isEmpty {
                Text(message).font(.callout).foregroundColor(low ? .orange : .secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var suggestionSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("三项建议").font(.headline)
                Spacer()
                Button("全部按推荐开启") { actions.run("checkup", action: "checkup-apply", ids: pendingOn) }
                    .buttonStyle(.borderedProminent).disabled(pendingOn.isEmpty)
            }.padding(.bottom, 8)
            ForEach(suggestions) { row in
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: row.id == "caches" ? "square.stack.3d.up" : row.id == "clean" ? "sparkles" : "externaldrive")
                        .frame(width: 20).foregroundColor(.accentColor)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text(row.title).fontWeight(.medium)
                            if let bytes = row.bytes, bytes > 0 {
                                Text("约 \(byteCount(bytes))").font(.caption).foregroundColor(.secondary)
                            }
                        }
                        Text(row.reason).font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    Toggle("", isOn: Binding(get: { row.outcome == "已开启" }, set: { on in
                        if on { actions.run("checkup", action: "checkup-apply", ids: [row.id]) }
                        else if row.id == "caches" { confirmingRestore = true }
                        else { actions.run("checkup", action: "checkup-off", ids: [row.id]) }
                    })).toggleStyle(.switch).labelsHidden()
                        .disabled(!row.eligible && row.outcome != "已开启")
                }.padding(.vertical, 10)
                Divider()
            }
        }
    }

    private var found: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("找到的任务目录和外接盘").font(.headline)
            if rows("任务目录").isEmpty { Text("没找到 git 工作区，自动清理暂时没有可管的任务目录。").font(.caption).foregroundColor(.secondary) }
            ForEach(rows("任务目录") + rows("外接盘")) { row in
                HStack {
                    Image(systemName: row.category == "外接盘" ? "externaldrive" : "folder").foregroundColor(.secondary).frame(width: 20)
                    Text(row.title).lineLimit(1).truncationMode(.middle)
                    Text(row.outcome ?? "").font(.caption).foregroundColor(row.outcome == "不可用" ? .secondary : .accentColor)
                    Spacer()
                    Text(row.reason).font(.caption).foregroundColor(.secondary).lineLimit(1).truncationMode(.middle)
                }
            }
        }
    }

    private var spaceSection: some View {
        let spaces = rows("空间去哪了")
        let largest = max(1, spaces.compactMap(\.bytes).max() ?? 1)
        return VStack(alignment: .leading, spacing: 10) {
            Text("空间去哪了").font(.headline)
            ForEach(spaces) { row in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(row.title).fontWeight(.medium)
                        Text(row.outcome ?? "").font(.caption2).padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Capsule().stroke(Color.secondary.opacity(0.5)))
                        Spacer()
                        // Unmeasured on an external drive: the background checkup never reads it (macOS would ask).
                        Text(row.bytes.map { $0 == 0 ? "空" : byteCount($0) } ?? (row.outcome == "外接盘" ? "在外接盘" : "读不到"))
                            .monospacedDigit().foregroundColor(.secondary)
                    }
                    GeometryReader { g in
                        Capsule().fill(Color.accentColor.opacity(0.55))
                            .frame(width: max(2, g.size.width * CGFloat(Double(row.bytes ?? 0) / Double(largest))))
                    }.frame(height: 4)
                    Text(row.reason).font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            DisclosureGroup("各工具缓存明细") {
                ForEach(rows("工具缓存")) { row in
                    HStack(alignment: .top) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(row.title)
                            Text(row.detail).font(.caption).foregroundColor(.secondary).lineLimit(1).truncationMode(.middle)
                        }
                        Spacer()
                        VStack(alignment: .trailing, spacing: 2) {
                            Text(row.bytes.map(byteCount) ?? "—").monospacedDigit().foregroundColor(.secondary)
                            Text(row.outcome ?? "").font(.caption)
                        }
                    }.padding(.vertical, 4)
                }
            }.font(.callout)
        }
    }
}

func number0(_ value: Double?) -> String { value.map { String(format: "%.0f", $0) } ?? "—" }

extension Snapshot {
    /// Free-space colour for the menu bar and the checkup page (space-watch AC-01).
    var freeLevel: Int {
        guard let free else { return 0 }
        if let critical, free < critical { return 2 }
        if let warning, free < warning { return 1 }
        return 0
    }
    var tintForFree: Color { [Color(red: 0.40, green: 0.85, blue: 0.69), .orange, .red][freeLevel] }
}
