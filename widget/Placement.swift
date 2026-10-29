import AppKit

/// One display as the cover sees it, in global screen coordinates (origin at the bottom-left of the main display).
struct PanelScreen {
    let frame: CGRect
    let visible: CGRect
    /// The span hidden by the camera housing, if any: a menu-bar icon there cannot be seen.
    var notch: ClosedRange<CGFloat>? = nil
}

extension PanelScreen {
    init(_ screen: NSScreen) {
        frame = screen.frame; visible = screen.visibleFrame
        if #available(macOS 12, *), let left = screen.auxiliaryTopLeftArea, let right = screen.auxiliaryTopRightArea, left.maxX < right.minX {
            notch = left.maxX...right.minX
        }
    }
}

/// Where the cover drops down when there are several displays. The menu-bar icon sits in one menu bar
/// at a time, but people open the cover on the display they are looking at, so the pointer's display decides.
enum PanelPlacement: Equatable {
    /// Drop from the icon itself: it is visible on the pointer's display.
    case icon
    /// Drop from this spot instead: the top of the pointer's display, as far from the right edge as the icon.
    case anchor(CGPoint)

    static let margin: CGFloat = 8

    /// The display under the pointer; on a shared edge or just outside every display, the nearest one.
    static func screen(at pointer: CGPoint, in screens: [PanelScreen]) -> PanelScreen? {
        screens.first { $0.frame.contains(pointer) } ?? screens.min { distance(pointer, $0.frame) < distance(pointer, $1.frame) }
    }

    /// `icon` is the menu-bar icon in screen coordinates, or nil when its menu bar is hidden (full screen, auto-hide).
    static func choose(screens: [PanelScreen], pointer: CGPoint, icon: CGRect?, width: CGFloat) -> PanelPlacement {
        guard let target = screen(at: pointer, in: screens) else { return .icon }
        var offset: CGFloat = 0
        if let icon, let home = screens.first(where: { $0.frame.insetBy(dx: -1, dy: -1).contains(icon) }),
           !(home.notch?.contains(icon.midX) ?? false) {
            if home.frame == target.frame { return .icon }
            offset = home.frame.maxX - icon.midX
        }
        let half = width / 2 + margin
        let x = min(max(target.frame.maxX - offset, target.visible.minX + half), target.visible.maxX - half)
        return .anchor(CGPoint(x: x, y: target.visible.maxY))
    }

    private static func distance(_ point: CGPoint, _ rect: CGRect) -> CGFloat {
        let dx = max(rect.minX - point.x, 0, point.x - rect.maxX), dy = max(rect.minY - point.y, 0, point.y - rect.maxY)
        return dx * dx + dy * dy
    }

    /// Fixed layouts for the self-test: main 1920×1080 with a 30 pt menu bar and the icon near its right edge.
    static func selfTest() -> Bool {
        let main = PanelScreen(frame: CGRect(x: 0, y: 0, width: 1920, height: 1080), visible: CGRect(x: 0, y: 90, width: 1920, height: 960))
        let right = PanelScreen(frame: CGRect(x: 1920, y: 0, width: 2560, height: 1440), visible: CGRect(x: 1920, y: 0, width: 2560, height: 1415))
        let left = PanelScreen(frame: CGRect(x: -2560, y: 0, width: 2560, height: 1440), visible: CGRect(x: -2560, y: 0, width: 2560, height: 1415))
        let above = PanelScreen(frame: CGRect(x: 0, y: 1080, width: 2560, height: 1440), visible: CGRect(x: 0, y: 1080, width: 2560, height: 1415))
        let narrow = PanelScreen(frame: CGRect(x: 4480, y: 0, width: 800, height: 600), visible: CGRect(x: 4480, y: 0, width: 800, height: 575))
        let laptop = PanelScreen(frame: CGRect(x: 0, y: 0, width: 1512, height: 982), visible: CGRect(x: 0, y: 0, width: 1512, height: 944), notch: 662...850)
        let icon = CGRect(x: 1700, y: 1050, width: 30, height: 30)   // 205 pt from the main display's right edge
        let cases: [(PanelPlacement, PanelPlacement)] = [
            (choose(screens: [main, right], pointer: CGPoint(x: 900, y: 500), icon: icon, width: 300), .icon),
            // Clicked right at the top edge: still the icon's display.
            (choose(screens: [main], pointer: CGPoint(x: 1715, y: 1080), icon: icon, width: 300), .icon),
            // Opened from another display: the same spot under that display's menu bar.
            (choose(screens: [main, right], pointer: CGPoint(x: 3000, y: 700), icon: icon, width: 300), .anchor(CGPoint(x: 4275, y: 1415))),
            (choose(screens: [main, left], pointer: CGPoint(x: -1000, y: 700), icon: icon, width: 300), .anchor(CGPoint(x: -205, y: 1415))),
            (choose(screens: [main, above], pointer: CGPoint(x: 100, y: 1500), icon: icon, width: 300), .anchor(CGPoint(x: 2355, y: 2495))),
            // A small display: the panel stays fully on it.
            (choose(screens: [main, right, narrow], pointer: CGPoint(x: 4600, y: 300), icon: CGRect(x: 285, y: 1050, width: 30, height: 30), width: 300),
             .anchor(CGPoint(x: 4638, y: 575))),
            // Menu bar hidden (full screen or auto-hide): top-right of the display in use.
            (choose(screens: [main, right], pointer: CGPoint(x: 900, y: 500), icon: nil, width: 300), .anchor(CGPoint(x: 1762, y: 1050))),
            // Icon pushed behind the camera housing: same, it cannot be pointed at.
            (choose(screens: [laptop], pointer: CGPoint(x: 700, y: 500), icon: CGRect(x: 690, y: 950, width: 30, height: 32), width: 300),
             .anchor(CGPoint(x: 1354, y: 944))),
        ]
        return cases.allSatisfy { $0.0 == $0.1 }
            && screen(at: CGPoint(x: 1920, y: 10), in: [main, right])?.frame == right.frame
    }
}
