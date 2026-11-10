// The parts of capture that touch no grant: redaction and retention. Kept
// apart from Capture.swift so the tests can compile them without
// ScreenCaptureKit or Accessibility.
//
// Order is fixed: the raw image exists only in this process's memory, is
// redacted, and only the redacted image is encoded, hashed, held or sent.
// No unredacted pixel is ever written anywhere.
//
// Retention: the helper holds the redacted captures it delivered, so the
// user can see what the agent saw (menu bar), and nothing else does. They
// are held in this process's memory, never on disk. Every file this user
// can write, Python can write too; this process's memory it cannot, since
// the hardened runtime refuses debugger attach to a same-user process. The
// caps are constants, not settings.

import CoreGraphics
import CryptoKit
import Foundation
import ImageIO
import UniformTypeIdentifiers

let maxHeldCaptures = 10
let heldCaptureSeconds: TimeInterval = 300
/// Longest edge of a delivered image, in pixels. Bounds what one capture
/// can carry.
let maxCaptureEdge: CGFloat = 1568

struct HeldCapture {
    let id: String
    let at: Date
    let target: String
    let png: Data
    let sha256: String
}

final class CaptureRetention {
    private let lock = NSLock()
    private var held: [HeldCapture] = []
    let now: () -> Date

    init(now: @escaping () -> Date = Date.init) { self.now = now }

    func add(_ c: HeldCapture) {
        lock.lock(); defer { lock.unlock() }
        held.append(c)
        prune()
    }

    /// Oldest first, after dropping anything past the caps.
    func current() -> [HeldCapture] {
        lock.lock(); defer { lock.unlock() }
        prune()
        return held
    }

    func clear() -> Int {
        lock.lock(); defer { lock.unlock() }
        let n = held.count
        held.removeAll()
        return n
    }

    private func prune() {
        let t = now()
        held.removeAll { t.timeIntervalSince($0.at) >= heldCaptureSeconds }
        if held.count > maxHeldCaptures { held.removeFirst(held.count - maxHeldCaptures) }
    }
}

/// Map rects in global screen points (top-left origin) into pixel rects of
/// an image whose top-left is ``origin`` and which has ``scale`` pixels per
/// point. Each rect is grown by ``pad`` points and clipped to the image;
/// rects entirely outside it are dropped.
func pixelRects(_ rects: [CGRect], origin: CGPoint, scale: CGFloat, imageSize: CGSize, pad: CGFloat = 2) -> [CGRect] {
    let bounds = CGRect(origin: .zero, size: imageSize)
    return rects.compactMap { r in
        let g = r.insetBy(dx: -pad, dy: -pad)
        let p = CGRect(x: (g.minX - origin.x) * scale, y: (g.minY - origin.y) * scale,
                       width: g.width * scale, height: g.height * scale).integral
        let c = p.intersection(bounds)
        return c.isNull || c.isEmpty ? nil : c
    }
}

/// A copy of ``image`` with each rect (pixel coordinates, top-left origin)
/// filled solid black. Nil if the copy cannot be made, which callers treat
/// as "do not deliver".
func redact(_ image: CGImage, _ rects: [CGRect]) -> CGImage? {
    let w = image.width, h = image.height
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpace(name: CGColorSpace.sRGB)!,
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
    ctx.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 1))
    for r in rects {
        // CGContext's origin is bottom-left; the rects are top-left.
        ctx.fill(CGRect(x: r.minX, y: CGFloat(h) - r.maxY, width: r.width, height: r.height))
    }
    return ctx.makeImage()
}

func encodePNG(_ image: CGImage) -> Data? {
    let out = NSMutableData()
    guard let dest = CGImageDestinationCreateWithData(out, UTType.png.identifier as CFString, 1, nil) else { return nil }
    CGImageDestinationAddImage(dest, image, nil)
    return CGImageDestinationFinalize(dest) ? out as Data : nil
}

func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}
