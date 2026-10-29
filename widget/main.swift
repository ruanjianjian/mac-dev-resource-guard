import AppKit
import SwiftUI
import Darwin
import UserNotifications
import Combine

struct Snapshot {
    var pressure: UInt32? = nil
    var ram: Double = 0
    var compressed: Double? = nil
    var swap: Double? = nil
    var free: Double? = nil
    var capacity: Double? = nil
    var target: Double? = nil
    var warning: Double? = nil
    var critical: Double? = nil
    var slots = 0
    var guardStatus = "未连接内存守护"
    /// When the memory monitor saw this pressure episode begin (nil when memory is fine).
    var memorySince: Double? = nil
    /// 0 fine, 1 tight (pressure 2), 2 very tight (pressure 4): shown in the menu bar and on the cover.
    var memoryLevel: Int { memorySince == nil ? 0 : pressure == 4 ? 2 : pressure == 2 ? 1 : 0 }
    var pending = 0
    var notice: (count: Int, critical: Bool)? = nil
    var agentReason: String? = nil
    var at = Date()
    var errors: [String] = []
    var pressureLabel: String {
        switch pressure { case 1: return "内存正常"; case 2: return "内存有压力"; case 4: return "内存压力高"; default: return "内存状态未知" }
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

/// Pending items the user still has to decide on (design C4); unreachable ones cannot run now.
func pendingCount(_ body: [String: Any]) -> Int {
    (body["items"] as? [[String: Any]] ?? []).filter {
        ($0["state"] as? String) == "pending" && ($0["unreachable"] as? Bool) != true
    }.count
}

/// The isolated UI test home when given, otherwise the user's home.
func stateHome() -> URL {
    if let home = try? testHomeArgument(CommandLine.arguments) { return URL(fileURLWithPath: home) }
    return FileManager.default.homeDirectoryForCurrentUser
}
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
    s.warning = (config["warning_free_gib"] as? NSNumber)?.doubleValue
    s.critical = (config["critical_free_gib"] as? NSNumber)?.doubleValue
    s.target = (config["target_free_gib"] as? NSNumber)?.doubleValue
        ?? (plan["target_free_bytes"] as? NSNumber).map { $0.doubleValue / 1_073_741_824 }
    s.slots = ((try? FileManager.default.contentsOfDirectory(atPath: state.appendingPathComponent("slots").path)) ?? []).filter { $0.hasSuffix(".json") }.count
    let memory = home.appendingPathComponent(".local/state/dev-memory")
    let memoryState = readJSON(memory.appendingPathComponent("state.json"))
    if FileManager.default.fileExists(atPath: memory.appendingPathComponent("paused").path) { s.guardStatus = "自动回收已暂停" }
    else if let last = memoryState["last_ts"] as? Double, abs(Date().timeIntervalSince1970 - last) < 180 { s.guardStatus = "内存守护正在观测" }
    else if !memoryState.isEmpty { s.guardStatus = "内存守护暂无近期观测" }
    s.memorySince = memoryState["incident_started"] as? Double
    let tiers = stateHome().appendingPathComponent(".local/state/dev-resource-guard")
    s.pending = pendingCount(readJSON(tiers.appendingPathComponent("pending.json")))
    let request = tiers.appendingPathComponent("notify-request.json")
    let notice = readJSON(request)
    if let count = notice["count"] as? Int {
        s.notice = (count, (notice["reason"] as? String) == "critical")
        try? FileManager.default.removeItem(at: request)   // one notice per request (design C6)
    }
    let agent = tiers.appendingPathComponent("agent-request.json")
    if let reason = readJSON(agent)["reason"] as? String {
        s.agentReason = reason
        try? FileManager.default.removeItem(at: agent)
    }
    return s
}

final class Monitor: ObservableObject {
    @Published var snapshot = Snapshot()
    @Published var loading = true
    /// What the background is moving or deleting right now (nil when idle); drives the motion.
    @Published var work: Work? { didSet { pace() } }
    private var fast: Timer?
    @Published var shortcut = "正在注册快捷键"
    private var timer: Timer?
    private var busy = false
    func start() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.refresh() }
        timer?.tolerance = 3
    }
    /// While the background works, sample every 2 s so the freed space counts up; back to 15 s after.
    private func pace() {
        if work != nil, fast == nil {
            fast = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
        } else if work == nil, fast != nil {
            fast?.invalidate(); fast = nil; refresh()
        }
    }
    func refresh() {
        guard !busy else { return }
        busy = true
        DispatchQueue.global(qos: .utility).async {
            let value = sample()
            DispatchQueue.main.async {
                self.snapshot = value; self.loading = false; self.busy = false
                (NSApp.delegate as? AppDelegate)?.updateStatus(value)
                if let notice = value.notice { (NSApp.delegate as? AppDelegate)?.notify(notice.count, critical: notice.critical) }
                if let reason = value.agentReason {
                    (NSApp.delegate as? AppDelegate)?.post(title: "有新占用需要判断", body: reason + "。点一下交给 agent。", tab: "agent")
                }
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate, UNUserNotificationCenterDelegate {
    let model = Monitor()
    let store = CoverStore()
    let watcher = WorkWatcher()
    let alerts = AlertWatcher()
    private var watching: [AnyCancellable] = []
    /// The menu-bar number rolls towards the new value; the dot after it pulses while something runs.
    private var shownFree: Double?
    private var roll: Timer?
    private var pulse: Timer?
    private var pulseOn = true
    var panel: NSPopover!
    /// An invisible spot on the display in use, for when the icon is not in that display's menu bar.
    let anchor = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 2, height: 1), styleMask: [.borderless], backing: .buffered, defer: true)
    var status: NSStatusItem!
    let statusMenu = NSMenu()
    var shortcut: GlobalShortcut?
    /// --probe-panel: open the cover once for this pointer spot (optionally as if the menu bar were hidden), report, quit.
    var probe: (pointer: CGPoint, hideIcon: Bool)?
    let badge = NSView(frame: NSRect(x: 14, y: 13, width: 7, height: 7))
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .darkAqua)
        NSApp.setActivationPolicy(.accessory)
        panel = NSPopover()
        panel.behavior = .transient
        panel.appearance = NSAppearance(named: .aqua)
        panel.contentSize = NSSize(width: 300, height: 340)
        panel.contentViewController = NSHostingController(rootView: CoverPanel(model: model, store: store))
        panel.delegate = self
        anchor.isOpaque = false; anchor.backgroundColor = .clear; anchor.hasShadow = false
        anchor.ignoresMouseEvents = true; anchor.isReleasedWhenClosed = false
        anchor.level = .statusBar
        anchor.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        status.button?.image = Logo.glyph()
        status.button?.imagePosition = .imageLeading
        status.button?.setAccessibilityLabel("显示资源看板")
        status.button?.target = self
        status.button?.action = #selector(statusClicked)
        status.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        badge.wantsLayer = true
        badge.layer?.backgroundColor = NSColor.systemRed.cgColor
        badge.layer?.cornerRadius = 3.5
        badge.isHidden = true
        status.button?.addSubview(badge)
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
        shortcut = GlobalShortcut { [weak self] in self?.togglePanel() }
        shortcut?.register()
        model.shortcut = shortcut?.label ?? "未注册，可从菜单栏打开"
        // Everything lives in the cover; the menu only opens it or quits (RAW-36).
        let menu = statusMenu
        menu.addItem(withTitle: "打开资源看板", action: #selector(show), keyEquivalent: "").target = self
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
        watcher.changed = { [weak self] work in self?.model.work = work }
        watcher.start()
        alerts.deliver = { [weak self] title, body, done in self?.post(title: title, body: body, tab: "memory", done: done) }
        alerts.start()
        // Either the background or the panel working lights the dot.
        watching = [model.$work.map { _ in () }.merge(with: store.$working.map { _ in () }).receive(on: DispatchQueue.main)
            .sink { [weak self] in self?.setBusy() }]
        model.start()
        // A drive in use was unplugged: say what stopped working right away (external-drive AC-02).
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didUnmountNotification, object: nil, queue: .main) { [weak self] note in
            guard let volume = (note.userInfo?[NSWorkspace.volumeURLUserInfoKey] as? URL)?.path else { return }
            DispatchQueue.global(qos: .utility).async {
                guard let reply = try? DashboardActions.call("checkup-status", path: volume), reply.status == "ok",
                      let row = reply.rows?.first else { return }
                DispatchQueue.main.async { self?.post(title: row.title, body: row.reason, tab: "checkup") }
            }
        }
        // After an install or upgrade: one checkup in the background, then one notice (one-line-install AC-05).
        // An automatic update says 「已更新到 …」 instead, so the user hears about it once, not twice (AC-11).
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String
        let updated = UpdateNotice.pending(version: version)
        if let updated, let version {
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
                self?.post(title: updated.title, body: updated.body, tab: "cover") { delivered in
                    if delivered { UpdateNotice.markAnnounced(version) }
                }
            }
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 8) { [weak self] in
            guard let reply = try? DashboardActions.call("checkup-firstrun"), reply.status == "ok", updated == nil else { return }
            DispatchQueue.main.async { self?.post(title: "体检完成", body: reply.message, tab: "checkup") }
        }
        if let probe { runProbe(probe) }
    }
    func runProbe(_ probe: (pointer: CGPoint, hideIcon: Bool)) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            self.showPanel(page: .cover, pointer: probe.pointer)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
                let shown = self.panel.contentViewController?.view.window?.frame
                let icon = self.status.button.flatMap { $0.window?.convertToScreen($0.convert($0.bounds, to: nil)) }
                print("screens \(NSScreen.screens.map { $0.frame })")
                print("icon \(icon.map { "\($0)" } ?? "none") · anchor \(self.anchor.isVisible ? "\(self.anchor.frame)" : "unused")")
                let ok = self.panel.isShown && shown != nil
                print("panel \(ok ? "\(shown!)" : "not shown") on \(self.panelScreen.map { "\($0)" } ?? "no display")")
                self.panel.close(); exit(ok ? 0 : 1)
            }
        }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { false }
    func updateStatus(_ s: Snapshot) {
        let waiting = s.pending > 0 ? " · \(s.pending) 项待确认" : ""
        status.button?.toolTip = "资源看板 · \(s.pressureLabel) · 磁盘可用 \(number(s.free)) GiB\(waiting)\n点击显示 · \(model.shortcut) 唤起 · 右键打开菜单"
        badge.isHidden = s.pending == 0
        watcher.read()
        alerts.read()
        rollFree(to: s.free)
        badge.frame.origin.x = 14
    }
    private var busy: Bool { model.work != nil || !store.working.isEmpty }
    /// The number moves from the old value to the new one in 0.6 s instead of jumping (no motion when reduced).
    private func rollFree(to free: Double?) {
        roll?.invalidate()
        guard let free, let start = shownFree, abs(free - start) >= 0.5, !Motion.reduced else { shownFree = free; renderTitle(); return }
        var step = 0.0
        roll = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] timer in
            step += 1
            let progress = min(1, step / 12)
            self?.shownFree = start + (free - start) * Motion.easeOut(progress); self?.renderTitle()
            if progress >= 1 { timer.invalidate() }
        }
    }
    private func setBusy() {
        if busy, pulse == nil, !Motion.reduced {
            pulse = Timer.scheduledTimer(withTimeInterval: 0.6, repeats: true) { [weak self] _ in self?.pulseOn.toggle(); self?.renderTitle() }
        } else if !busy {
            pulse?.invalidate(); pulse = nil; pulseOn = true
        }
        renderTitle()
    }
    /// Free space beside the icon; orange below the warning line, red below the critical line.
    private func renderTitle() {
        let font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.systemFontSize - 1, weight: .medium)
        let colour: NSColor = [NSColor.labelColor, .systemOrange, .systemRed][model.snapshot.freeLevel]
        let title = NSMutableAttributedString(string: shownFree.map { String(format: " %.0fG", $0) } ?? "",
                                              attributes: [.foregroundColor: colour, .font: font])
        // Memory under pressure stays visible next to the number until it eases.
        let memory = model.snapshot.memoryLevel
        if memory > 0 {
            title.append(NSAttributedString(string: memory == 2 ? " 内存很紧" : " 内存紧张",
                                            attributes: [.font: font, .foregroundColor: memory == 2 ? NSColor.systemRed : NSColor.systemOrange]))
        }
        if busy {
            title.append(NSAttributedString(string: " •", attributes: [.font: font,
                .foregroundColor: NSColor.labelColor.withAlphaComponent(pulseOn ? 1 : 0.25)]))
        }
        status.button?.attributedTitle = title
    }
    func post(title: String, body: String, tab: String, done: ((Bool) -> Void)? = nil) {
        let content = UNMutableNotificationContent()
        content.title = title; content.body = body
        content.userInfo = ["tab": tab]
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "\(tab)-\(Date().timeIntervalSince1970)", content: content, trigger: nil)) { error in
            DispatchQueue.main.async { done?(error == nil) }
        }
    }
    func notify(_ count: Int, critical: Bool) {
        let content = UNMutableNotificationContent()
        content.title = "有 \(count) 项清理等你确认"
        content.body = (critical ? "磁盘可用空间已低于低水位。" : "") + "打开「待确认」查看大小、理由和会失去什么。"
        content.userInfo = ["tab": "pending"]
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "pending-\(Date().timeIntervalSince1970)", content: content, trigger: nil))
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler done: @escaping () -> Void) {
        let tab = response.notification.request.content.userInfo["tab"] as? String ?? "pending"
        DispatchQueue.main.async {
            if tab == "agent" { self.store.handOff() }
            self.showPanel(page: tab == "pending" ? .pending : tab == "agent" ? .plan : tab == "memory" ? .memory : .cover)
        }
        done()
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .list, .sound])
    }
    @objc func statusClicked() {
        if let event = NSApp.currentEvent, event.type == .rightMouseUp, let button = status.button {
            NSMenu.popUpContextMenu(statusMenu, with: event, for: button)
        } else { togglePanel() }
    }
    @objc func show() { showPanel(page: .cover) }
    /// The cover drops down on the display under the pointer: from the menu-bar icon when it is there,
    /// otherwise from the same spot under that display's menu bar. Data loads only when it is opened.
    func showPanel(page: CoverPage, pointer: CGPoint = NSEvent.mouseLocation) {
        guard let button = status.button else { return }
        store.page = page
        store.refresh()
        model.refresh()
        let screens = NSScreen.screens.map(PanelScreen.init)
        if panel.isShown {
            // Open on another display: bring it over. Open here already: only the page changes.
            guard panelScreen != PanelPlacement.screen(at: pointer, in: screens)?.frame else { NSApp.activate(ignoringOtherApps: true); return }
            panel.close()
        }
        let seen = !(probe?.hideIcon ?? false) && (button.window?.occlusionState.contains(.visible) ?? false)
        let icon = seen ? button.window?.convertToScreen(button.convert(button.bounds, to: nil)) : nil
        switch PanelPlacement.choose(screens: screens, pointer: pointer, icon: icon, width: panel.contentSize.width) {
        case .icon:
            panel.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        case .anchor(let spot):
            anchor.setFrame(NSRect(x: spot.x - 1, y: spot.y - 1, width: 2, height: 1), display: false)
            anchor.orderFrontRegardless()
            panel.show(relativeTo: anchor.contentView!.bounds, of: anchor.contentView!, preferredEdge: .minY)
        }
        NSApp.activate(ignoringOtherApps: true)
    }
    var panelScreen: CGRect? { panel.contentViewController?.view.window?.screen?.frame }
    func popoverDidClose(_ notification: Notification) { if !panel.isShown { anchor.orderOut(nil) } }
    /// The shortcut on another display moves the cover there instead of closing it.
    func togglePanel() {
        let here = PanelPlacement.screen(at: NSEvent.mouseLocation, in: NSScreen.screens.map(PanelScreen.init))?.frame
        panel.isShown && panelScreen == here ? panel.performClose(nil) : showPanel(page: .cover)
    }
    @objc func refresh() { model.refresh() }
    @objc func quit() { NSApp.terminate(nil) }
}

/// Isolated UI evidence: draw one panel tab into a PNG; no window is ever shown on screen.
/// Isolated evidence for the menu-bar cover: cover, cover-pending, cover-space, cover-switches.
/// RENDER_FREE / RENDER_CAPACITY / RENDER_PENDING pin the numbers so pictures do not depend on this Mac's disk.
func renderCover(_ tab: String, to out: String) throws {
    let env = ProcessInfo.processInfo.environment
    let model = Monitor(); model.snapshot = sample(); model.loading = false
    if let free = env["RENDER_FREE"].flatMap(Double.init) { model.snapshot.free = free }
    if let capacity = env["RENDER_CAPACITY"].flatMap(Double.init) { model.snapshot.capacity = capacity }
    if let pending = env["RENDER_PENDING"].flatMap(Int.init) { model.snapshot.pending = pending }
    if let parts = env["RENDER_WORK"]?.split(separator: ":").map(String.init), parts.count == 4 {
        model.work = Work(kind: parts[0], title: parts[1], bytes: Int64(parts[2]) ?? 0, freeBefore: Double(parts[3]))
    }
    let store = CoverStore()
    store.checkup = try DashboardActions.call("checkup-preview")
    store.pending = try DashboardActions.call("pending-preview")
    store.plan = try DashboardActions.call("plan-preview")
    store.memory = try DashboardActions.call("memory-page")
    if let pressure = env["RENDER_PRESSURE"].flatMap(UInt32.init) {
        model.snapshot.pressure = pressure; model.snapshot.memorySince = Date().timeIntervalSince1970 - 360
    }
    store.page = ["cover-pending": .pending, "cover-space": .space, "cover-switches": .switches, "cover-plan": .plan,
                  "cover-memory": .memory][tab] ?? .cover
    if let message = store.checkup?.message, !message.isEmpty { print("体检 · " + message) }
    for row in store.checkup?.rows ?? [] where ["建议", "提醒", "外接盘"].contains(row.category) { print("\(row.title) → \(row.outcome ?? "") · \(row.reason)") }
    for row in store.pendingRows { print("待确认 · \(row.title) · \(row.reason)") }
    let view = NSHostingView(rootView: CoverPanel(model: model, store: store))
    view.frame = NSRect(x: 0, y: 0, width: 300, height: 340)
    let window = NSWindow(contentRect: view.frame, styleMask: [.borderless], backing: .buffered, defer: false)
    window.appearance = NSAppearance(named: .aqua)
    window.contentView = view
    view.layoutSubtreeIfNeeded()
    RunLoop.current.run(until: Date().addingTimeInterval(0.8))
    guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { throw NSError(domain: "Dashboard", code: 4) }
    view.cacheDisplay(in: view.bounds, to: rep)
    try rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
}

func require(_ condition: Bool) { if !condition { fputs("Self-test failed\n", stderr); exit(1) } }

if CommandLine.arguments.contains("--run-tick") {
    // launchd runs the storage tick through the app: macOS then asks once whether 资源看板 may use the
    // external drive, instead of silently refusing a background command-line job.
    let task = Process()
    task.executableURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/resource-guard-service")
    task.arguments = ["tick-storage"]
    do { try task.run(); task.waitUntilExit(); exit(task.terminationStatus) }
    catch { fputs("resource-guard tick did not start: \(error)\n", stderr); exit(2) }
} else if CommandLine.arguments.contains("--self-test") {
    var s = Snapshot(); require(s.pressureLabel == "内存状态未知")
    for (raw,label) in [(UInt32(1),"内存正常"),(2,"内存有压力"),(4,"内存压力高")] { s.pressure=raw; require(s.pressureLabel == label) }
    require(number(nil) == "—"); require(gib(1_073_741_824) == 1)
    let live=sample(); require(live.ram>0); require(live.free != nil); require(live.capacity! >= live.free!)
    var attempts = 0
    let fallback = ShortcutChoice.firstAvailable { _ in attempts += 1; return attempts == 2 }
    require(fallback?.label == "⌃⌥⌘R"); require(attempts == 2)
    require(ShortcutChoice.firstAvailable { _ in false } == nil)
    require(ShortcutChoice.choices.allSatisfy { $0.label != "⌘S" })
    require(Motion.selfTest())
    var memory = Snapshot(); memory.pressure = 2; require(memory.memoryLevel == 0)   // no episode recorded yet
    memory.memorySince = 1; require(memory.memoryLevel == 1); memory.pressure = 4; require(memory.memoryLevel == 2)
    memory.pressure = 1; require(memory.memoryLevel == 0)
    require(pendingCount(["items": [["state": "pending"], ["state": "ignored"], ["state": "pending", "unreachable": true], ["state": "failed"]]]) == 1)
    require(pendingCount([:]) == 0)
    var space = Snapshot(); space.free = 80; space.warning = 69; space.critical = 46; require(space.freeLevel == 0)
    space.free = 50; require(space.freeLevel == 1); space.free = 21; require(space.freeLevel == 2)
    space.free = nil; require(space.freeLevel == 0)
    let pendingRow = try! JSONDecoder().decode(ResourceRow.self, from: Data(#"{"id":"simulator-delete:U","title":"t","detail":"","category":"待确认","bytes":1,"eligible":true,"reason":"r","outcome":"待确认","command":null,"kind":"simulator-delete","loses":"登录状态"}"#.utf8))
    require(pendingRow.loses == "登录状态" && pendingRow.kind == "simulator-delete")
    let oldRow = try! JSONDecoder().decode(ResourceRow.self, from: Data(#"{"id":"a","title":"t","detail":"","category":"c","bytes":null,"eligible":false,"reason":"r","outcome":null,"command":null}"#.utf8))
    require(oldRow.loses == nil && oldRow.tag == nil)
    let memoryRow = try! JSONDecoder().decode(ResourceRow.self, from: Data(#"{"id":"proc:7","title":"node","detail":"","category":"正在用","bytes":1,"eligible":false,"reason":"在用 · 不动","outcome":null,"command":null,"tag":"use"}"#.utf8))
    require(memoryRow.tag == "use")
    require(PanelPlacement.selfTest())
    require(Logo.selfTest())
    require(UpdateNotice.selfTest())
    require(try! testHomeArgument(["app"]) == nil)
    require(try! testHomeArgument(["app", "--test-home", "/fixture"]) == "/fixture")
    for args in [["app", "--test-home"], ["app", "--test-home", "--other"], ["app", "--test-home", ""]] {
        do { _ = try testHomeArgument(args); require(false) } catch { }
    }
    print("PASS: pressure states, missing values, units, read-only sampling, preview gating, shortcut conflict fallback, isolated-home argument rejection, pending count and row decoding, free-space colour levels, panel placement across displays, motion, logo, update notice")
} else if let index = CommandLine.arguments.firstIndex(of: "--render-icon"), index + 1 < CommandLine.arguments.count {
    // The build packs this folder into AppIcon.icns, so the icon is drawn from the same code as the menu-bar mark.
    do { try Logo.writeIconset(to: URL(fileURLWithPath: CommandLine.arguments[index + 1])) }
    catch { fputs("icon render failed: \(error)\n", stderr); exit(1) }
} else if let index = CommandLine.arguments.firstIndex(of: "--render-tab"), index + 2 < CommandLine.arguments.count {
    // Only against a marked fixture home, so evidence never reads or changes real data.
    guard let home = try? testHomeArgument(CommandLine.arguments), FileManager.default.fileExists(atPath: home + "/.resource-dashboard-fixture") else {
        fputs("render needs --test-home with a fixture marker\n", stderr); exit(2)
    }
    _ = NSApplication.shared
    NSApp.setActivationPolicy(.prohibited)
    do { try renderCover(CommandLine.arguments[index + 1], to: CommandLine.arguments[index + 2]) }
    catch { fputs("render failed: \(error)\n", stderr); exit(1) }
} else {
    let app = NSApplication.shared
    let delegate = AppDelegate(); app.delegate = delegate
    if let index = CommandLine.arguments.firstIndex(of: "--probe-panel"), index + 1 < CommandLine.arguments.count {
        let spot = CommandLine.arguments[index + 1].split(separator: ",").compactMap { Double($0) }
        guard spot.count == 2 else { fputs("--probe-panel needs x,y in screen points\n", stderr); exit(2) }
        delegate.probe = (CGPoint(x: spot[0], y: spot[1]), CommandLine.arguments.contains("--hide-icon"))
    }
    withExtendedLifetime(delegate) { app.run() }
}
