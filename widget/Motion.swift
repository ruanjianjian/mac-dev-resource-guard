import AppKit
import SwiftUI

/// What the background is moving or deleting right now, from working.json (written by the storage tick).
struct Work: Equatable {
    let kind: String          // move / prune / clean
    let title: String
    let bytes: Int64
    let freeBefore: Double?   // GiB free on the internal disk when it started

    /// Space freed since it started, measured against the number on the cover.
    func freed(_ free: Double?) -> Double { max(0, (free ?? 0) - (freeBefore ?? free ?? 0)) }

    /// A file older than 15 minutes is a tick that died half-way: nothing is running.
    static func read(_ body: [String: Any], now: Date = Date()) -> Work? {
        guard let kind = body["kind"] as? String, let at = body["at"] as? Double,
              now.timeIntervalSince1970 - at < 900 else { return nil }
        return Work(kind: kind, title: body["title"] as? String ?? "", bytes: (body["bytes"] as? NSNumber)?.int64Value ?? 0,
                    freeBefore: body["free_before_gib"] as? Double)
    }
}

/// Watches the state folder, so the motion starts the moment the background starts, not at the next sample.
final class WorkWatcher {
    var changed: (Work?) -> Void = { _ in }
    private var source: DispatchSourceFileSystemObject?
    private var last: Work?
    private var folder: URL { stateHome().appendingPathComponent(".local/state/dev-resource-guard") }

    func start() {
        let fd = open(folder.path, O_EVTONLY)
        if fd >= 0 {
            let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .rename, .delete], queue: .main)
            source.setEventHandler { [weak self] in self?.read() }
            source.setCancelHandler { close(fd) }
            source.resume()
            self.source = source
        }
        read()
    }

    /// Also called on every sample, in case the folder could not be watched.
    func read() {
        let work = Work.read(readJSON(folder.appendingPathComponent("working.json")))
        if work != last { last = work; changed(work) }
    }
}

enum Motion {
    /// The system's 「减少动态效果」: everything then changes in place, without moving.
    static var reduced: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }
    static func easeOut(_ progress: Double) -> Double { 1 - pow(1 - progress, 3) }

    static func selfTest() -> Bool {
        let now = Date()
        let moving = Work.read(["kind": "move", "at": now.timeIntervalSince1970 - 5, "title": "pay-flow", "bytes": 1 << 30, "free_before_gib": 40.0], now: now)
        let stale = Work.read(["kind": "move", "at": now.timeIntervalSince1970 - 901], now: now)
        return moving?.title == "pay-flow" && moving?.freed(43.5) == 3.5 && moving?.freed(39) == 0 && stale == nil
            && Work.read([:]) == nil && easeOut(0) == 0 && easeOut(1) == 1 && easeOut(0.5) > 0.5
    }
}

/// A number that counts to its new value instead of jumping.
struct RollingNumber: View, Animatable {
    var value: Double
    var format = "%.0f"
    var animatableData: Double {
        get { value }
        set { value = newValue }
    }
    var body: some View { Text(String(format: format, value)).monospacedDigit() }
}

/// Three dots flowing left to right: something is moving to the external drive.
struct FlowDots: View {
    var color: Color
    var body: some View {
        if Motion.reduced {
            Canvas { context, size in draw(context, size, phase: 0) }
        } else {
            TimelineView(.animation) { timeline in
                Canvas { context, size in draw(context, size, phase: timeline.date.timeIntervalSinceReferenceDate / 1.8) }
            }
        }
    }
    private func draw(_ context: GraphicsContext, _ size: CGSize, phase: Double) {
        for index in 0..<3 {
            let at = (phase + Double(index) / 3).truncatingRemainder(dividingBy: 1)
            let fade = Motion.reduced ? 1 : min(1, min(at, 1 - at) * 6)
            context.fill(Path(ellipseIn: CGRect(x: size.width * at - 2, y: size.height / 2 - 2, width: 4, height: 4)),
                         with: .color(color.opacity(fade)))
        }
    }
}

/// A soft highlight going round the ring while the background works.
struct SweepArc: View {
    var lineWidth: CGFloat
    var body: some View {
        TimelineView(.animation) { timeline in
            let turn = timeline.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 2.4) / 2.4
            Circle().trim(from: 0, to: 0.1).stroke(Color.white.opacity(0.3), lineWidth: lineWidth)
                .rotationEffect(.degrees(turn * 360 - 90))
        }
    }
}
