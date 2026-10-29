import AppKit
import ImageIO
import SwiftUI
import UniformTypeIdentifiers

/// 小留: a small robot that tidies up. Its eye is the cover's ring with a gap left open (the space it keeps free),
/// and it carries one block of cache. One drawing serves the app icon, the menu-bar mark and the README pictures.
enum Logo {
    static let navy = rgb(0x1D2B53), navyTop = rgb(0x26376A), navyBottom = rgb(0x18234A)
    static let cream = rgb(0xF3E9C6), creamTop = rgb(0xF7F0D6), creamBottom = rgb(0xEADCAF)
    static let warm = rgb(0xC0613F)

    static func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
        CGColor(srgbRed: CGFloat(hex >> 16 & 0xFF) / 255, green: CGFloat(hex >> 8 & 0xFF) / 255, blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
    }

    /// The eye: 80% of a ring, open from about ten to twelve o'clock like the free part of the cover ring.
    static func eye(_ path: CGMutablePath, center: CGPoint, radius: CGFloat) {
        path.addArc(center: center, radius: radius, startAngle: -.pi / 2, endAngle: -.pi / 2 + 2 * .pi * 0.8, clockwise: false)
    }

    /// The full-colour icon on Apple's 1024 grid: an 824-point rounded square with room for its shadow.
    static func drawIcon(_ ctx: CGContext, side: CGFloat) {
        ctx.saveGState()
        ctx.translateBy(x: 0, y: side); ctx.scaleBy(x: side / 1024, y: -side / 1024)   // 1024 grid, y down
        let tile = RoundedRectangle(cornerRadius: 185.4, style: .continuous).path(in: CGRect(x: 100, y: 100, width: 824, height: 824)).cgPath
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: -side * 0.012), blur: side * 0.028, color: rgb(0x000000, 0.32))
        ctx.addPath(tile); ctx.setFillColor(navy); ctx.fillPath()
        ctx.restoreGState()
        ctx.saveGState()
        ctx.addPath(tile); ctx.clip()
        vertical(ctx, navyTop, navyBottom, from: 100, to: 924)
        ctx.restoreGState()
        ctx.translateBy(x: 100, y: 100); ctx.scaleBy(x: 824 / 160, y: 824 / 160)        // the 160 drawing grid
        robot(ctx)
        ctx.restoreGState()
    }

    private static func robot(_ ctx: CGContext) {
        ctx.setLineCap(.round)
        ctx.setFillColor(rgb(0x0B1230, 0.28))                                              // ground shadow
        ctx.fillEllipse(in: CGRect(x: 32, y: 138.5, width: 68, height: 7))
        ctx.setFillColor(cream)
        ctx.addPath(CGPath(roundedRect: CGRect(x: 38, y: 117, width: 56, height: 9), cornerWidth: 4.5, cornerHeight: 4.5, transform: nil))
        ctx.fillPath()
        for x in [48.0, 84.0] {                                                           // wheels with hubs
            ctx.setFillColor(cream); ctx.fillEllipse(in: CGRect(x: x - 8, y: 124, width: 16, height: 16))
            ctx.setFillColor(navy); ctx.fillEllipse(in: CGRect(x: x - 2.6, y: 129.4, width: 5.2, height: 5.2))
        }
        stroke(ctx, cream, width: 5, from: CGPoint(x: 66, y: 42), to: CGPoint(x: 66, y: 29))  // antenna
        ctx.setFillColor(warm); ctx.fillEllipse(in: CGRect(x: 59, y: 17, width: 14, height: 14))
        stroke(ctx, cream, width: 7, from: CGPoint(x: 104, y: 93), to: CGPoint(x: 117, y: 93))  // arm
        ctx.saveGState()                                                                    // body
        ctx.addPath(CGPath(roundedRect: CGRect(x: 26, y: 42, width: 80, height: 72), cornerWidth: 20, cornerHeight: 20, transform: nil))
        ctx.clip()
        vertical(ctx, creamTop, creamBottom, from: 42, to: 114)
        ctx.restoreGState()
        let ring = CGMutablePath(); eye(ring, center: CGPoint(x: 66, y: 78), radius: 21)
        ctx.addPath(ring); ctx.setStrokeColor(navy); ctx.setLineWidth(10); ctx.strokePath()
        ctx.setFillColor(navy); ctx.fillEllipse(in: CGRect(x: 63, y: 72, width: 12, height: 12))
        ctx.setFillColor(cream); ctx.fillEllipse(in: CGRect(x: 69.2, y: 73.7, width: 3.6, height: 3.6))
        ctx.saveGState()                                                                    // the block it carries, lifted a little
        ctx.translateBy(x: 126, y: 92); ctx.rotate(by: -0.12)
        ctx.setFillColor(warm)
        ctx.addPath(CGPath(roundedRect: CGRect(x: -13, y: -13, width: 26, height: 26), cornerWidth: 6, cornerHeight: 6, transform: nil))
        ctx.fillPath()
        stroke(ctx, rgb(0xF3E9C6, 0.6), width: 2.8, from: CGPoint(x: -6.5, y: -5), to: CGPoint(x: 6.5, y: -5))
        ctx.restoreGState()
    }

    private static func stroke(_ ctx: CGContext, _ colour: CGColor, width: CGFloat, from: CGPoint, to: CGPoint) {
        ctx.setStrokeColor(colour); ctx.setLineWidth(width)
        ctx.move(to: from); ctx.addLine(to: to); ctx.strokePath()
    }

    private static func vertical(_ ctx: CGContext, _ top: CGColor, _ bottom: CGColor, from: CGFloat, to: CGFloat) {
        let gradient = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB), colors: [top, bottom] as CFArray, locations: [0, 1])!
        ctx.drawLinearGradient(gradient, start: CGPoint(x: 0, y: from), end: CGPoint(x: 0, y: to), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
    }

    /// The menu-bar mark: the icon's body filled with the eye cut out, as a template image so macOS tints it for
    /// light, dark and highlighted bars. One bitmap per scale, so 1x screens get edges on whole pixels too.
    static func glyph() -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18))
        for scale in [1, 2] {
            guard let pixels = glyphBitmap(scale: scale) else { continue }
            let rep = NSBitmapImageRep(cgImage: pixels)
            rep.size = image.size
            image.addRepresentation(rep)
        }
        image.isTemplate = true
        image.accessibilityDescription = "资源看板"
        return image
    }

    static func glyphBitmap(scale: Int) -> CGImage? {
        bitmap(18 * scale, 18 * scale) { ctx in
            ctx.translateBy(x: 0, y: CGFloat(18 * scale)); ctx.scaleBy(x: CGFloat(scale), y: -CGFloat(scale))
            drawGlyph(ctx, retina: scale > 1)
        }
    }

    static func drawGlyph(_ ctx: CGContext, retina: Bool) {
        ctx.setFillColor(.black)
        ctx.addPath(CGPath(roundedRect: CGRect(x: 2, y: 5, width: 14, height: 9), cornerWidth: 3, cornerHeight: 3, transform: nil))
        ctx.fillPath()
        ctx.setBlendMode(.clear); ctx.setLineWidth(retina ? 1.5 : 1.4); ctx.setLineCap(.round)
        let ring = CGMutablePath(); eye(ring, center: CGPoint(x: 9, y: 9.5), radius: retina ? 2.4 : 2.3)
        ctx.addPath(ring); ctx.strokePath()
        ctx.setBlendMode(.normal)
        ctx.fill(retina ? CGRect(x: 8.5, y: 2.5, width: 1, height: 3) : CGRect(x: 8, y: 3, width: 2, height: 2.5))   // antenna
        ctx.fillEllipse(in: retina ? CGRect(x: 7.6, y: 0.6, width: 2.8, height: 2.8) : CGRect(x: 7.5, y: 0.5, width: 3, height: 3))
        for x in [5.5, 12.5] { ctx.fillEllipse(in: CGRect(x: x - 1.5, y: retina ? 14.75 : 15, width: 3, height: 3)) }   // wheels
    }

    static func bitmap(_ width: Int, _ height: Int, _ draw: (CGContext) -> Void) -> CGImage? {
        guard let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        draw(ctx)
        return ctx.makeImage()
    }

    static func png(_ image: CGImage, to url: URL) throws {
        guard let out = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else { throw NSError(domain: "Logo", code: 1) }
        CGImageDestinationAddImage(out, image, nil)
        if !CGImageDestinationFinalize(out) { throw NSError(domain: "Logo", code: 2) }
    }

    /// Every size an .iconset needs; `iconutil -c icns` packs the folder at build time.
    static func writeIconset(to folder: URL) throws {
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        for base in [16, 32, 128, 256, 512] {
            for scale in [1, 2] {
                let side = base * scale
                guard let image = bitmap(side, side, { drawIcon($0, side: CGFloat(side)) }) else { throw NSError(domain: "Logo", code: 3) }
                try png(image, to: folder.appendingPathComponent("icon_\(base)x\(base)\(scale == 2 ? "@2x" : "").png"))
            }
        }
    }

    /// Reads back a few pixels: transparent outside the tile, navy on it, cream on the body, the open eye, and the glyph's cut-out eye.
    static func selfTest() -> Bool {
        guard let icon = bitmap(160, 160, { drawIcon($0, side: 160) }),
              let data = icon.dataProvider?.data, let bytes = CFDataGetBytePtr(data) else { return false }
        func pixel(_ x: Int, _ y: Int) -> (r: Int, g: Int, b: Int, a: Int) {
            let i = y * icon.bytesPerRow + x * 4
            return (Int(bytes[i]), Int(bytes[i + 1]), Int(bytes[i + 2]), Int(bytes[i + 3]))
        }
        let corner = pixel(2, 2), tile = pixel(24, 80), body = pixel(Int(100 + 36 * 5.15) * 160 / 1024, Int(100 + 100 * 5.15) * 160 / 1024)
        let gap = pixel(Int(100 + (66 - 13) * 5.15) * 160 / 1024, Int(100 + (78 - 15) * 5.15) * 160 / 1024)
        let ring = pixel(Int(100 + (66 + 21) * 5.15) * 160 / 1024, Int(100 + 78 * 5.15) * 160 / 1024)
        guard corner.a == 0, tile.a == 255, tile.b > tile.r + 30,
              body.r > 200, body.g > 190, gap.r > 200, ring.r < 80 else { return false }
        let mark = glyph()
        guard mark.isTemplate, mark.size == NSSize(width: 18, height: 18), mark.representations.count == 2,
              let fine = glyphBitmap(scale: 2), let fineData = fine.dataProvider?.data else { return false }
        let ink = CFDataGetBytePtr(fineData)!
        func alpha(_ x: CGFloat, _ y: CGFloat) -> Int { Int(ink[Int(y * 2) * fine.bytesPerRow + Int(x * 2) * 4 + 3]) }
        // Body beside the eye, the eye ring cut out, its gap left filled, air between the body and a wheel.
        return alpha(3.5, 9.5) > 200 && alpha(11.4, 9.5) < 60 && alpha(7.6, 7.6) > 200 && alpha(5.5, 14.4) < 60
    }
}
