// Pointer input: desktop.input.pointer (click, scroll). No keyboard
// events are built anywhere in this file.
//
// A pointer action names a pixel of a capture the helper still holds,
// never a raw screen position: the agent can only click on something it
// was shown, in the app it was shown, within the capture's retention
// window. The helper maps the pixel to the screen itself.
//
// Checked before every single event, not once per action (runSequence's
// ``check``):
// * the kernel's re-check: every never-approvable guard (kill switch armed,
//   secure input off, the front app identified and the target, no
//   password field focused, nothing blocklisted frontmost) and the grant
//   still live;
// * no app was activated since the action was authorised;
// * the window under the point belongs to the target app (so a dialog,
//   popup or another app's window that appeared over it is not clicked).
// Human input trips the kill switch between any two events.
//
// Deliberate, not a bug to fix: if a stop lands between a button's down
// and up, the up is not posted. Posting it would complete a click after
// the stop, which is worse than a button that reads as held; the user's
// own next click releases it.

import AppKit
import CoreGraphics
import Foundation

/// The owner of the frontmost visible window containing ``p`` (global
/// points, top-left origin), or nil if none can be identified.
func windowOwner(at p: CGPoint) -> pid_t? {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] else {
        return nil
    }
    for w in list {   // front to back
        guard let b = w[kCGWindowBounds as String] as? [String: Any],
              let rect = CGRect(dictionaryRepresentation: b as CFDictionary), rect.contains(p) else { continue }
        if let alpha = w[kCGWindowAlpha as String] as? Double, alpha <= 0 { continue }
        return w[kCGWindowOwnerPID as String] as? pid_t
    }
    return nil
}

enum PointerAction {
    case click(button: CGMouseButton, count: Int)
    case scroll(dx: Int32, dy: Int32)

    var audit: [String: Any] {
        switch self {
        case .click(let b, let n): return ["kind": "click", "button": b == .right ? "right" : "left", "count": n]
        case .scroll(let dx, let dy): return ["kind": "scroll", "dx": Int(dx), "dy": Int(dy)]
        }
    }
}

func pointerSteps(_ action: PointerAction, at p: CGPoint) -> [Step] {
    var steps: [Step] = []
    func mouse(_ type: CGEventType, _ button: CGMouseButton, clickState: Int = 0) {
        guard let e = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: p, mouseButton: button) else { return }
        if clickState > 0 { e.setIntegerValueField(.mouseEventClickState, value: Int64(clickState)) }
        steps.append(.post(e))
    }
    mouse(.mouseMoved, .left)
    steps.append(.wait(0.05))
    switch action {
    case .click(let button, let count):
        let (down, up): (CGEventType, CGEventType) = button == .right ? (.rightMouseDown, .rightMouseUp)
                                                                     : (.leftMouseDown, .leftMouseUp)
        for i in 1...count {
            mouse(down, button, clickState: i)
            steps.append(.wait(0.03))
            mouse(up, button, clickState: i)
            if i < count { steps.append(.wait(0.06)) }
        }
    case .scroll(let dx, let dy):
        if let e = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0) {
            e.location = p
            steps.append(.post(e))
        }
    }
    return steps
}

func pointerVerb(kernel: Kernel, target: Target, captureId: String, x: Double, y: Double,
                 action: PointerAction, requester: Requester) -> Result<[String: Any], CaptureError> {
    var args = action.audit
    args["capture"] = captureId
    args["x"] = x
    args["y"] = y
    func refuse(_ why: String) -> Result<[String: Any], CaptureError> {
        kernel.log("action_denied", ["class": Desktop.pointer, "target": target.id, "args": args, "reason": why,
                                     "caller": requester.audit])
        return .failure(CaptureError(why))
    }
    guard let held = captureRetention.find(captureId) else {
        return refuse("no held screenshot \(captureId) (they are deleted after \(Int(heldCaptureSeconds))s); capture again")
    }
    guard held.target == target.id else { return refuse("screenshot \(captureId) is of \(held.target), not \(target.id)") }
    guard let point = globalPoint(x: x, y: y, in: held) else { return refuse("(\(x), \(y)) is outside screenshot \(captureId)") }
    args["screen_point"] = ["x": point.x, "y": point.y]

    let request = ActionRequest(classId: Desktop.pointer, target: target, args: args)
    let focus = FocusWatch.shared.generation
    let seq: Int, grantId: String
    switch kernel.authorize(request, requester: requester) {
    case .allow(let s, let g): (seq, grantId) = (s, g)
    case .deny(let why): return .failure(CaptureError(why))
    }
    guard let app = frontmostApplication(), app.bundleIdentifier == target.id else {
        kernel.recordOutcome(seq: seq, outcome: "not_started", fields: ["reason": "the target is no longer frontmost"])
        return .failure(CaptureError("the target is no longer frontmost"))
    }
    let pid = app.processIdentifier
    let check: () -> String? = {
        if FocusWatch.shared.generation != focus { return "another app was activated" }
        if let why = kernel.stillAllowed(request, grantId: grantId) { return why }
        guard windowOwner(at: point) == pid else { return "the point is no longer over a \(target.label) window" }
        return nil
    }
    let r = runSequence(pointerSteps(action, at: point), check: check)
    let outcome = r.interrupted == nil ? "completed" : "interrupted"
    kernel.recordOutcome(seq: seq, outcome: outcome, fields: [
        "events_posted": r.completed, "steps": r.total, "reason": r.interrupted ?? NSNull(), "restored": r.restored])
    guard r.interrupted == nil else {
        return .failure(CaptureError("\(outcome) after \(r.completed) of \(r.total) steps: \(r.interrupted!)"))
    }
    return .success(["completed": r.completed, "total": r.total, "audit_seq": seq])
}
