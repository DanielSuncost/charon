// Capture: desktop.observe.screen, decided by the kernel like any action.
//
// A screenshot is the highest-leakage verb, so:
// * The kernel's never-approvable guards apply exactly as to input: no
//   capture while secure input is on, a blocklisted app is frontmost, a
//   secure text field has focus, the front app is unknown or not the
//   granted target, or the screen is locked.
// * Only the target app's own windows are composited (a ScreenCaptureKit
//   filter that *includes* that app's windows). Every other app's pixels
//   are never rendered into the image: this helper's approval dialog and
//   menu, a password manager's popup floating above the target, a
//   notification, the menu bar and Dock. Windows owned by this process are
//   also dropped explicitly, in case the filter is ever widened.
// * Secure text fields in the target's windows are found through
//   Accessibility, before and after the shot, and blacked out before the
//   image is encoded. If the walk cannot finish (the tree is too big or the
//   app stops answering), nothing is delivered: fail closed.
// * After the shot the kernel re-checks every guard and the grant, and the
//   helper checks that no app was activated in between. If anything moved,
//   the image is discarded unseen.
//
// * Capture runs only while the kill switch is armed (user idle), and must
//   state a local destination, which the dialog shows and the audit keeps.
//   Where the image really goes once the caller has it is beyond this
//   process: that destination is a claim on record, not a boundary.
//
// The redacted PNG goes to the authenticated caller and into the helper's
// in-memory retention (CaptureCore.swift). The audit records its hash and
// size, never the pixels.

import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import ScreenCaptureKit

/// Counts app activations, so a capture can tell whether focus moved while
/// it was being taken. Observed on the main run loop (AppKit is running).
final class FocusWatch {
    static let shared = FocusWatch()
    private let lock = NSLock()
    private var _generation = 0
    private var token: NSObjectProtocol?

    var generation: Int { lock.lock(); defer { lock.unlock() }; return _generation }

    func start() {
        token = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: nil
        ) { [weak self] _ in
            guard let self = self else { return }
            self.lock.lock(); self._generation += 1; self.lock.unlock()
        }
    }
}

let captureRetention = CaptureRetention()

struct CaptureError: Error, CustomStringConvertible {
    let description: String
    init(_ d: String) { description = d }
}

// ── Secure fields ───────────────────────────────────────────────────

struct SecureFieldScan {
    var rects: [CGRect] = []
    var visited = 0
    var complete = true
    var problem: String?
}

/// Every AXSecureTextField in the app's windows, as global-point rects.
/// Bounded in elements and time; running out is "incomplete", which the
/// caller refuses.
func scanSecureFields(pid: pid_t, budget: Int = 6000, deadline: TimeInterval = 2.0) -> SecureFieldScan {
    var scan = SecureFieldScan()
    guard AXIsProcessTrusted() else {
        scan.complete = false; scan.problem = "Accessibility is not granted"; return scan
    }
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.5)
    let until = Date().addingTimeInterval(deadline)
    var stack: [AXUIElement] = []
    var windows: CFTypeRef?
    let wr = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &windows)
    if wr == .success, let ws = windows as? [AXUIElement] {
        stack = ws
    } else if wr != .noValue {
        scan.complete = false; scan.problem = "the app's windows cannot be read (\(wr.rawValue))"; return scan
    }
    let attrs = [kAXRoleAttribute, kAXSubroleAttribute, kAXPositionAttribute, kAXSizeAttribute,
                 kAXChildrenAttribute] as CFArray
    let secure = kAXSecureTextFieldSubrole as String
    while let el = stack.popLast() {
        scan.visited += 1
        if scan.visited > budget || Date() > until {
            scan.complete = false
            scan.problem = "could not check the whole window for password fields (\(scan.visited) elements)"
            return scan
        }
        AXUIElementSetMessagingTimeout(el, 0.5)
        var values: CFArray?
        let r = AXUIElementCopyMultipleAttributeValues(el, attrs, AXCopyMultipleAttributeOptions(rawValue: 0), &values)
        if r == .cannotComplete {
            scan.complete = false; scan.problem = "the app stopped answering Accessibility"; return scan
        }
        guard r == .success, let v = values as? [Any], v.count == 5 else { continue }
        let role = v[0] as? String, subrole = v[1] as? String
        if role == secure || subrole == secure {
            var origin = CGPoint.zero, size = CGSize.zero
            if CFGetTypeID(v[2] as CFTypeRef) == AXValueGetTypeID() { AXValueGetValue(v[2] as! AXValue, .cgPoint, &origin) }
            if CFGetTypeID(v[3] as CFTypeRef) == AXValueGetTypeID() { AXValueGetValue(v[3] as! AXValue, .cgSize, &size) }
            if size.width > 0 && size.height > 0 {
                scan.rects.append(CGRect(origin: origin, size: size))
            } else {
                // A password field we cannot place cannot be blacked out.
                scan.complete = false; scan.problem = "a password field has no readable position"; return scan
            }
            continue
        }
        if let children = v[4] as? [AXUIElement] { stack.append(contentsOf: children) }
    }
    return scan
}

// ── ScreenCaptureKit ────────────────────────────────────────────────

private func shareableContent() throws -> SCShareableContent {
    let done = DispatchSemaphore(value: 0)
    var result: Result<SCShareableContent, Error> = .failure(CaptureError("no answer from ScreenCaptureKit"))
    SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: true) { content, error in
        if let c = content { result = .success(c) } else { result = .failure(error ?? CaptureError("no content")) }
        done.signal()
    }
    guard done.wait(timeout: .now() + 5) == .success else { throw CaptureError("ScreenCaptureKit did not answer") }
    return try result.get()
}

private func screenshot(_ filter: SCContentFilter, _ config: SCStreamConfiguration) throws -> CGImage {
    let done = DispatchSemaphore(value: 0)
    var result: Result<CGImage, Error> = .failure(CaptureError("no answer from ScreenCaptureKit"))
    SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) { image, error in
        if let i = image { result = .success(i) } else { result = .failure(error ?? CaptureError("no image")) }
        done.signal()
    }
    guard done.wait(timeout: .now() + 5) == .success else { throw CaptureError("ScreenCaptureKit did not answer") }
    return try result.get()
}

struct Shot {
    let image: CGImage
    let origin: CGPoint      // global points of the image's top-left
    let scale: CGFloat       // pixels per point
    let windows: Int
}

/// The target app's on-screen windows on the display holding most of
/// them, and nothing else.
func shootApp(pid: pid_t) throws -> Shot {
    let content = try shareableContent()
    let own = getpid()
    let mine = content.windows.filter {
        $0.owningApplication?.processID == pid && $0.owningApplication?.processID != own
            && $0.isOnScreen && $0.frame.width >= 2 && $0.frame.height >= 2
    }
    guard !mine.isEmpty else { throw CaptureError("the target has no window on screen") }
    func overlap(_ d: SCDisplay) -> CGFloat {
        mine.reduce(0) { acc, w in
            let i = w.frame.intersection(d.frame)
            return acc + (i.isNull ? 0 : i.width * i.height)
        }
    }
    guard let display = content.displays.max(by: { overlap($0) < overlap($1) }), overlap(display) > 0 else {
        throw CaptureError("the target's windows are on no display")
    }
    let onDisplay = mine.filter { !$0.frame.intersection(display.frame).isNull }
    let union = onDisplay.dropFirst().reduce(onDisplay[0].frame) { $0.union($1.frame) }.intersection(display.frame)
    let filter = SCContentFilter(display: display, including: onDisplay)
    let scale = min(CGFloat(filter.pointPixelScale), maxCaptureEdge / max(union.width, union.height))
    let config = SCStreamConfiguration()
    config.sourceRect = union.offsetBy(dx: -display.frame.minX, dy: -display.frame.minY)
    config.width = max(1, Int((union.width * scale).rounded()))
    config.height = max(1, Int((union.height * scale).rounded()))
    config.showsCursor = false
    config.capturesAudio = false
    let image = try screenshot(filter, config)
    // The image may come back at a slightly different size; derive the
    // scale actually delivered so redaction lands where it should.
    return Shot(image: image, origin: union.origin, scale: CGFloat(image.width) / union.width, windows: onDisplay.count)
}

// ── The verb ────────────────────────────────────────────────────────

/// Authorise, shoot, re-check, redact, hold, deliver. Any failure after
/// the shot discards the image; nothing unredacted leaves this function.
func captureVerb(kernel: Kernel, target: Target, terms: [String: String],
                 requester: Requester) -> Result<[String: Any], CaptureError> {
    let request = ActionRequest(classId: Desktop.observeScreen, target: target, args: [:], terms: terms)
    let focusBefore = FocusWatch.shared.generation
    let seq: Int, grantId: String
    switch kernel.authorize(request, requester: requester) {
    case .allow(let s, let g): (seq, grantId) = (s, g)
    case .deny(let why): return .failure(CaptureError(why))
    }
    func discard(_ why: String) -> Result<[String: Any], CaptureError> {
        kernel.recordOutcome(seq: seq, outcome: "discarded", fields: ["reason": why])
        return .failure(CaptureError("capture discarded: \(why)"))
    }
    guard let app = frontmostApplication(), app.bundleIdentifier == target.id else {
        return discard("the target is no longer frontmost")
    }
    let before = scanSecureFields(pid: app.processIdentifier)
    guard before.complete else { return discard(before.problem ?? "password-field check incomplete") }
    let shot: Shot
    do { shot = try shootApp(pid: app.processIdentifier) } catch { return discard("\(error)") }
    // Everything below runs on an image nobody has seen yet.
    if FocusWatch.shared.generation != focusBefore { return discard("another app was activated during the capture") }
    if let why = kernel.stillAllowed(request, grantId: grantId) { return discard(why) }
    guard let front = frontmostApplication(), front.processIdentifier == app.processIdentifier else {
        return discard("the target is no longer frontmost")
    }
    let after = scanSecureFields(pid: app.processIdentifier)
    guard after.complete else { return discard(after.problem ?? "password-field check incomplete") }
    let size = CGSize(width: shot.image.width, height: shot.image.height)
    let rects = pixelRects(before.rects + after.rects, origin: shot.origin, scale: shot.scale, imageSize: size)
    guard let clean = redact(shot.image, rects), let png = encodePNG(clean) else {
        return discard("redaction failed")
    }
    let digest = sha256Hex(png)
    let id = "c-" + String(digest.prefix(12))
    captureRetention.add(HeldCapture(id: id, at: Date(), target: target.id, png: png, sha256: digest,
                                     origin: shot.origin, scale: shot.scale, width: clean.width, height: clean.height))
    kernel.recordOutcome(seq: seq, outcome: "captured", fields: [
        "capture": id, "sha256": digest, "width": clean.width, "height": clean.height, "bytes": png.count,
        "windows": shot.windows, "redacted_fields": rects.count, "stated_destination": terms])
    return .success([
        "id": id, "png_base64": png.base64EncodedString(), "sha256": digest,
        "width": clean.width, "height": clean.height,
        "origin": ["x": shot.origin.x, "y": shot.origin.y], "pixels_per_point": shot.scale,
        "redacted_fields": rects.count, "audit_seq": seq,
    ])
}
