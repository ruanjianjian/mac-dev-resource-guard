import AppKit
import SwiftUI
import Darwin

struct Snapshot {
    var pressure: UInt32? = nil
    var ram: Double = 0
    var compressed: Double? = nil
    var swap: Double? = nil
    var free: Double? = nil
    var capacity: Double? = nil
    var target: Double? = nil
    var slots = 0
    var guardStatus = "未连接内存守护"
    var at = Date()
    var errors: [String] = []
    var pressureLabel: String {
        switch pressure { case 1: return "内存正常"; case 2: return "内存有压力"; case 4: return "内存压力高"; default: return "内存状态未知" }
    }
    var tint: Color {
        switch pressure { case 1: return Color(red: 0.40, green: 0.85, blue: 0.69); case 2: return .orange; case 4: return .red; default: return .secondary }
    }
}

func readJSON(_ path: URL) -> [String: Any] {
    // Only read small metadata, never cache contents, logs, account pools or argv.
    guard let attrs = try? FileManager.default.attributesOfItem(atPath: path.path),
          let size = attrs[.size] as? NSNumber, size.intValue <= 1_048_576,
          let data = try? Data(contentsOf: path),
          let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
    return result
}
func expanded(_ value: String) -> URL {
    URL(fileURLWithPath: (value as NSString).expandingTildeInPath)
}
func gib(_ value: UInt64) -> Double { Double(value) / 1_073_741_824 }
func number(_ value: Double?) -> String { value.map { String(format: "%.1f", $0) } ?? "—" }

func sample() -> Snapshot {
    var s = Snapshot()
    s.ram = gib(ProcessInfo.processInfo.physicalMemory)
    var pressure: UInt32 = 0
    var pressureSize = MemoryLayout<UInt32>.size
    if sysctlbyname("kern.memorystatus_vm_pressure_level", &pressure, &pressureSize, nil, 0) == 0 { s.pressure = pressure }
    else { s.errors.append("内存压力读取失败") }
    var swap = xsw_usage()
    var swapSize = MemoryLayout<xsw_usage>.size
    if sysctlbyname("vm.swapusage", &swap, &swapSize, nil, 0) == 0 { s.swap = gib(swap.xsu_used) }
    else { s.errors.append("交换空间读取失败") }
    let host = mach_host_self()
    defer { mach_port_deallocate(mach_task_self_, host) }
    var vm = vm_statistics64()
    var count = mach_msg_type_number_t(MemoryLayout<vm_statistics64>.size / MemoryLayout<integer_t>.size)
    let result = withUnsafeMutablePointer(to: &vm) { p in
        p.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { host_statistics64(host, HOST_VM_INFO64, $0, &count) }
    }
    var page: vm_size_t = 0
    if result == KERN_SUCCESS && host_page_size(host, &page) == KERN_SUCCESS {
        s.compressed = gib(UInt64(vm.compressor_page_count) * UInt64(page))
    } else { s.errors.append("压缩内存读取失败") }
    let home = FileManager.default.homeDirectoryForCurrentUser
    let config = readJSON(home.appendingPathComponent(".config/dev-resource-guard/config.json"))
    let state = expanded(config["state_root"] as? String ?? "~/.local/state/dev-storage")
    let volume = expanded(config["volume"] as? String ?? home.path)
    if let fs = try? FileManager.default.attributesOfFileSystem(forPath: volume.path),
       let free = fs[.systemFreeSize] as? NSNumber, let total = fs[.systemSize] as? NSNumber {
        s.free = free.doubleValue / 1_073_741_824; s.capacity = total.doubleValue / 1_073_741_824
    } else { s.errors.append("磁盘空间读取失败") }
    let plan = readJSON(state.appendingPathComponent("last-plan.json"))
    s.target = (config["target_free_gib"] as? NSNumber)?.doubleValue
        ?? (plan["target_free_bytes"] as? NSNumber).map { $0.doubleValue / 1_073_741_824 }
    s.slots = ((try? FileManager.default.contentsOfDirectory(atPath: state.appendingPathComponent("slots").path)) ?? []).filter { $0.hasSuffix(".json") }.count
    let memory = home.appendingPathComponent(".local/state/dev-memory")
    let memoryState = readJSON(memory.appendingPathComponent("state.json"))
    if FileManager.default.fileExists(atPath: memory.appendingPathComponent("paused").path) { s.guardStatus = "自动回收已暂停" }
    else if let last = memoryState["last_ts"] as? Double, abs(Date().timeIntervalSince1970 - last) < 180 { s.guardStatus = "内存守护正在观测" }
    else if !memoryState.isEmpty { s.guardStatus = "内存守护暂无近期观测" }
    return s
}

final class Monitor: ObservableObject {
    @Published var snapshot = Snapshot()
    @Published var loading = true
    @Published var pinned = UserDefaults.standard.bool(forKey: "pinned")
    @Published var shortcut = "正在注册快捷键"
    private var timer: Timer?
    private var busy = false
    func start() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.refresh() }
        timer?.tolerance = 3
    }
    func refresh() {
        guard !busy else { return }
        busy = true
        DispatchQueue.global(qos: .utility).async {
            let value = sample()
            DispatchQueue.main.async {
                self.snapshot = value; self.loading = false; self.busy = false
                (NSApp.delegate as? AppDelegate)?.updateStatus(value)
            }
        }
    }
}

struct Overview: View {
    @ObservedObject var model: Monitor
    var body: some View {
        let s = model.snapshot
        VStack(alignment: .leading, spacing: 22) {
            VStack(alignment: .leading, spacing: 16) {
                HStack(spacing: 8) {
                    Circle().fill(s.tint).frame(width: 8, height: 8)
                    Text(model.loading ? "正在读取…" : s.pressureLabel).font(.system(size: 17, weight: .medium))
                    Spacer()
                    Text("RAM").font(.system(size: 10, weight: .semibold)).foregroundColor(.secondary)
                }
                HStack(spacing: 0) {
                    metric("物理内存", number(s.ram))
                    Spacer()
                    metric("压缩内存", number(s.compressed))
                    Spacer()
                    metric("交换空间", number(s.swap))
                }
            }
            Divider()
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("磁盘可用").font(.system(size: 12, weight: .medium)).foregroundColor(.secondary)
                    Spacer()
                    Image(systemName: "internaldrive").foregroundColor(.secondary)
                }
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(number(s.free)).font(.system(size: 43, weight: .light, design: .rounded)).monospacedDigit()
                    Text("GiB").font(.system(size: 13)).foregroundColor(.secondary)
                    Spacer()
                }
                GeometryReader { g in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.primary.opacity(0.08))
                        Capsule().fill(((s.free ?? 0) < (s.target ?? 0) ? Color.orange : Color(red: 0.40, green: 0.85, blue: 0.69)).opacity(0.8)).frame(width: g.size.width * CGFloat(min(1, max(0, (s.free ?? 0) / max(1, s.capacity ?? 1)))))
                    }
                }.frame(height: 5).accessibilityLabel("磁盘可用比例")
                HStack {
                    Text("总容量 \(number(s.capacity)) GiB")
                    Spacer()
                    if let goal = s.target { Text("目标 \(String(format: "%.0f", goal)) GiB") }
                }.font(.system(size: 10)).foregroundColor(.secondary)
                Text("空间目标仅提醒 · 构建照常进行").font(.system(size: 11)).foregroundColor(.secondary)
            }
            Divider()
            VStack(alignment: .leading, spacing: 9) {
                Label(s.guardStatus, systemImage: "waveform.path").font(.system(size: 11))
                Label("已记录 \(s.slots) 个固定构建槽", systemImage: "square.stack.3d.up").font(.system(size: 11)).foregroundColor(.secondary)
                if !s.errors.isEmpty { Text(s.errors.joined(separator: " · ")).font(.system(size: 10)).foregroundColor(.orange).fixedSize(horizontal: false, vertical: true) }
            }
            HStack {
                Text(model.loading ? "读取中" : "更新于 \(s.at.formatted(date: .omitted, time: .standard))")
                    .font(.system(size: 10)).foregroundColor(.secondary)
                Spacer()
                Button("刷新") { model.refresh() }.buttonStyle(.borderless).font(.system(size: 11))
                Button("活动监视器") { NSWorkspace.shared.open(URL(fileURLWithPath: "/System/Applications/Utilities/Activity Monitor.app")) }
                    .buttonStyle(.borderless).font(.system(size: 11))
            }
        }
        .padding(24).frame(width: 340).background(Color(nsColor: .windowBackgroundColor))
    }
    private func metric(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.system(size: 10)).foregroundColor(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(value).font(.system(size: 19, weight: .medium, design: .rounded)).monospacedDigit()
                Text("G").font(.system(size: 9)).foregroundColor(.secondary)
            }
        }
    }

}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let model = Monitor()
    let actions = DashboardActions()
    var window: NSWindow!
    var status: NSStatusItem!
    let statusMenu = NSMenu()
    var shortcut: GlobalShortcut?
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .darkAqua)
        NSApp.setActivationPolicy(.accessory)
        let view = NSHostingView(rootView: Dashboard(model: model, actions: actions))
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 700), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "资源看板"; window.titlebarAppearsTransparent = true
        window.minSize = NSSize(width: 620, height: 700)
        window.contentView = view; window.delegate = self; window.isReleasedWhenClosed = false
        window.isRestorable = false
        window.setFrameAutosaveName("resource-dashboard")
        if !window.setFrameUsingName("resource-dashboard") { window.center() }
        window.level = model.pinned ? .floating : .normal
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        status.button?.image = NSImage(systemSymbolName: "gauge", accessibilityDescription: "资源看板")
        status.button?.image?.isTemplate = true
        status.button?.setAccessibilityLabel("显示资源看板")
        status.button?.target = self
        status.button?.action = #selector(statusClicked)
        status.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        shortcut = GlobalShortcut { [weak self] in self?.show() }
        shortcut?.register()
        model.shortcut = shortcut?.label ?? "未注册，可从菜单栏打开"
        let menu = statusMenu
        menu.addItem(withTitle: "显示资源看板", action: #selector(show), keyEquivalent: "").target = self
        menu.addItem(withTitle: "刷新", action: #selector(refresh), keyEquivalent: "r").target = self
        let shortcutItem = NSMenuItem(title: "全局唤起：\(model.shortcut)", action: nil, keyEquivalent: "")
        shortcutItem.isEnabled = false
        menu.addItem(shortcutItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出资源看板", action: #selector(quit), keyEquivalent: "q").target = self
        let mainMenu = NSMenu()
        let applicationItem = NSMenuItem()
        let applicationMenu = NSMenu()
        applicationMenu.addItem(withTitle: "显示资源看板", action: #selector(show), keyEquivalent: "1").target = self
        applicationMenu.addItem(withTitle: "退出资源看板", action: #selector(quit), keyEquivalent: "q").target = self
        applicationItem.submenu = applicationMenu
        mainMenu.addItem(applicationItem)
        NSApp.mainMenu = mainMenu
        model.start()
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { false }
    func updateStatus(_ s: Snapshot) {
        status.button?.toolTip = "资源看板 · \(s.pressureLabel) · 磁盘可用 \(number(s.free)) GiB\n点击显示 · \(model.shortcut) 唤起 · 右键打开菜单"
    }
    @objc func statusClicked() {
        if let event = NSApp.currentEvent, event.type == .rightMouseUp, let button = status.button {
            NSMenu.popUpContextMenu(statusMenu, with: event, for: button)
        } else { show() }
    }
    @objc func show() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func refresh() { model.refresh() }
    @objc func quit() {
        if actions.busy { show(); return }
        NSApp.terminate(nil)
    }
}

func require(_ condition: Bool) { if !condition { fputs("Self-test failed\n", stderr); exit(1) } }

if CommandLine.arguments.contains("--self-test") {
    var s = Snapshot(); require(s.pressureLabel == "内存状态未知")
    for (raw,label) in [(UInt32(1),"内存正常"),(2,"内存有压力"),(4,"内存压力高")] { s.pressure=raw; require(s.pressureLabel == label) }
    require(number(nil) == "—"); require(gib(1_073_741_824) == 1)
    let live=sample(); require(live.ram>0); require(live.free != nil); require(live.capacity! >= live.free!)
    var attempts = 0
    let fallback = ShortcutChoice.firstAvailable { _ in attempts += 1; return attempts == 2 }
    require(fallback?.label == "⌃⌥⌘R"); require(attempts == 2)
    require(ShortcutChoice.firstAvailable { _ in false } == nil)
    require(ShortcutChoice.choices.allSatisfy { $0.label != "⌘S" })
    let valid = BackendReply(status: "ok", message: "", rows: [], token: "test", created: Date().timeIntervalSince1970, eligible: 1, eligibleBytes: 0)
    require(valid.canClean)
    let stale = BackendReply(status: "ok", message: "", rows: [], token: "test", created: Date().timeIntervalSince1970 - 301, eligible: 1, eligibleBytes: 0)
    require(!stale.canClean)
    let failed = BackendReply(status: "busy", message: "", rows: [], token: "test", created: Date().timeIntervalSince1970, eligible: 1, eligibleBytes: 0)
    require(!failed.canClean)
    require(try! testHomeArgument(["app"]) == nil)
    require(try! testHomeArgument(["app", "--test-home", "/fixture"]) == "/fixture")
    for args in [["app", "--test-home"], ["app", "--test-home", "--other"], ["app", "--test-home", ""]] {
        do { _ = try testHomeArgument(args); require(false) } catch { }
    }
    print("PASS: pressure states, missing values, units, read-only sampling, preview gating, shortcut conflict fallback, isolated-home argument rejection")
} else {
    let app = NSApplication.shared
    let delegate = AppDelegate(); app.delegate = delegate
    withExtendedLifetime(delegate) { app.run() }
}
