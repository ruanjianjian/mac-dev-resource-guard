// Renders the README banner (light and dark) and a 1024 icon from widget/Logo.swift, so pictures never drift from the app.
//   xcrun swiftc -parse-as-library widget/Logo.swift scripts/render_brand.swift -o /tmp/render_brand && /tmp/render_brand docs/images
import AppKit

@main
enum Brand {
    static func main() throws {
        let folder = URL(fileURLWithPath: CommandLine.arguments.dropFirst().first ?? "docs/images")
        try Logo.png(Logo.bitmap(1024, 1024) { Logo.drawIcon($0, side: 1024) }!, to: folder.appendingPathComponent("icon.png"))
        try banner(dark: false, to: folder.appendingPathComponent("banner.png"))
        try banner(dark: true, to: folder.appendingPathComponent("banner-dark.png"))
    }

    /// 2560×1280 (2:1, also fits GitHub's social preview): 小留 on the left, name, English name and one line on what it does.
    static func banner(dark: Bool, to url: URL) throws {
        let image = Logo.bitmap(2560, 1280) { ctx in
            ctx.setFillColor(Logo.rgb(dark ? 0x0F1424 : 0xFAF8F4)); ctx.fill(CGRect(x: 0, y: 0, width: 2560, height: 1280))
            ctx.saveGState(); ctx.translateBy(x: 300, y: 320); Logo.drawIcon(ctx, side: 640); ctx.restoreGState()
            NSGraphicsContext.saveGraphicsState()
            ctx.translateBy(x: 0, y: 1280); ctx.scaleBy(x: 1, y: -1)
            NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: true)
            ctx.setFillColor(Logo.warm); ctx.fill(CGRect(x: 1064, y: 356, width: 112, height: 14))
            func line(_ text: String, _ font: NSFont, _ hex: UInt32, _ y: CGFloat, kern: CGFloat = 0) {
                NSAttributedString(string: text, attributes: [.font: font, .foregroundColor: NSColor(cgColor: Logo.rgb(hex))!, .kern: kern])
                    .draw(at: NSPoint(x: 1056, y: y))
            }
            let cjk = { (size: CGFloat, weight: String) in NSFont(name: "PingFangSC-" + weight, size: size) ?? .systemFont(ofSize: size) }
            line("资源看板", cjk(190, "Semibold"), dark ? 0xF3E9C6 : 0x1D2B53, 420, kern: 8)
            line("Mac Dev Resource Guard", .systemFont(ofSize: 72, weight: .medium), dark ? 0xB9C2DE : 0x6B7591, 690)
            line("共享构建缓存 · 用好外接盘 · 替你看住磁盘", cjk(64, "Regular"), dark ? 0xE6E2DA : 0x2B2A27, 836)
            NSGraphicsContext.restoreGraphicsState()
        }!
        try Logo.png(image, to: url)
    }
}
