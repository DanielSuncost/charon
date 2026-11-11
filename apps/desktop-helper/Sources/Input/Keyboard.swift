// Keyboard input: desktop.input.text (type characters) and
// desktop.input.shortcut (a fixed allowlist, KeyboardCore.swift).
//
// Like the pointer, every keyboard action names a pixel of a screenshot
// the helper still holds, and the focused element must belong to the
// target app and contain that point: the agent types into the field it
// was shown, not whatever happens to have focus.
//
// Before the first key:
// * text, and shortcuts that edit text, need a text-entry field (text
//   field, text area, editable combo box) to have focus;
// * text is refused if the field's label suggests a secret (heuristic),
//   and text or text-editing shortcuts are refused if the field's window
//   contains a password field anywhere (a login form or credential prompt,
//   whatever app shows it). An incomplete scan of that window refuses.
// Before every single key event (runSequence's ``check``):
// * the kernel's re-check: kill switch armed, secure input off, the front
//   app the target and identified, no password field focused, nothing
//   blocklisted frontmost, grant live;
// * no app activated since authorisation;
// * the same element still has focus;
// * for a letter shortcut, the key still produces that letter with Command
//   in the current keyboard layout.
// Human input trips the kill switch between any two events.
//
// The audit records how many characters were typed, never the text.
//
// A stop between a key's down and up leaves that key's up unposted, by
// design, as with the pointer: posting it after a stop would be acting
// after a stop. macOS clears it on the next real key press.

import AppKit
import ApplicationServices
import Carbon
import CoreGraphics
import Foundation

enum KeyboardAction {
    case text(String)
    case shortcut(Shortcut)

    var classId: String {
        if case .text = self { return Desktop.text }
        return Desktop.shortcut
    }

    var needsTextFocus: Bool {
        switch self {
        case .text: return true
        case .shortcut(let s): return s.needsTextFocus
        }
    }

    var audit: [String: Any] {
        switch self {
        case .text(let t): return ["kind": "text", "chars": t.count]
        case .shortcut(let s): return ["kind": "shortcut", "name": s.name]
        }
    }
}

struct Focused {
    let element: AXUIElement
    let pid: pid_t
    let role: String
    let frame: CGRect?
    let window: AXUIElement?
    let labels: [String]
}

func focusedElement() -> Focused? {
    guard AXIsProcessTrusted() else { return nil }
    let system = AXUIElementCreateSystemWide()
    AXUIElementSetMessagingTimeout(system, 1.0)
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(system, kAXFocusedUIElementAttribute as CFString, &value) == .success,
          let v = value, CFGetTypeID(v) == AXUIElementGetTypeID() else { return nil }
    let el = v as! AXUIElement
    var pid: pid_t = 0
    guard AXUIElementGetPid(el, &pid) == .success else { return nil }
    func attr(_ name: String) -> CFTypeRef? {
        var out: CFTypeRef?
        return AXUIElementCopyAttributeValue(el, name as CFString, &out) == .success ? out : nil
    }
    var frame: CGRect?
    if let p = attr(kAXPositionAttribute), let z = attr(kAXSizeAttribute),
       CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(z) == AXValueGetTypeID() {
        var origin = CGPoint.zero, size = CGSize.zero
        AXValueGetValue(p as! AXValue, .cgPoint, &origin)
        AXValueGetValue(z as! AXValue, .cgSize, &size)
        frame = CGRect(origin: origin, size: size)
    }
    var window: AXUIElement?
    if let w = attr(kAXWindowAttribute), CFGetTypeID(w) == AXUIElementGetTypeID() { window = (w as! AXUIElement) }
    let labels = [kAXTitleAttribute, kAXDescriptionAttribute, kAXPlaceholderValueAttribute, kAXHelpAttribute,
                  kAXIdentifierAttribute].compactMap { attr($0) as? String }.filter { !$0.isEmpty }
    return Focused(element: el, pid: pid, role: attr(kAXRoleAttribute) as? String ?? "", frame: frame,
                   window: window, labels: labels)
}

/// The character ``keyCode`` produces in the current keyboard layout with
/// Command held, or nil if it cannot be told (callers refuse).
func layoutCharacter(keyCode: UInt16) -> Character? {
    guard let source = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
          let raw = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData) else { return nil }
    let data = Unmanaged<CFData>.fromOpaque(raw).takeUnretainedValue() as Data
    var dead: UInt32 = 0
    var chars = [UniChar](repeating: 0, count: 4)
    var length = 0
    let status = data.withUnsafeBytes { buf -> OSStatus in
        guard let base = buf.baseAddress else { return -1 }
        return UCKeyTranslate(base.assumingMemoryBound(to: UCKeyboardLayout.self), keyCode, UInt16(kUCKeyActionDown),
                              UInt32((cmdKey >> 8) & 0xFF), UInt32(LMGetKbdType()),
                              OptionBits(kUCKeyTranslateNoDeadKeysBit), &dead, chars.count, &length, &chars)
    }
    guard status == noErr, length == 1 else { return nil }
    return String(utf16CodeUnits: chars, count: length).lowercased().first
}

func keyboardSteps(_ action: KeyboardAction) -> [Step] {
    var steps: [Step] = []
    func key(_ code: UInt16, down: Bool, flags: CGEventFlags, text: [UniChar]? = nil) {
        guard let e = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down) else { return }
        e.flags = flags
        if var t = text { e.keyboardSetUnicodeString(stringLength: t.count, unicodeString: &t) }
        steps.append(.post(e))
    }
    switch action {
    case .text(let s):
        for ch in s {
            let units = Array(String(ch).utf16)
            key(0, down: true, flags: [], text: units)
            key(0, down: false, flags: [], text: units)
            steps.append(.wait(0.012))
        }
    case .shortcut(let sc):
        key(sc.keyCode, down: true, flags: sc.flags)
        steps.append(.wait(0.02))
        key(sc.keyCode, down: false, flags: sc.flags)
    }
    return steps
}

func keyboardVerb(kernel: Kernel, target: Target, captureId: String, x: Double, y: Double,
                  action: KeyboardAction, requester: Requester) -> Result<[String: Any], CaptureError> {
    var args = action.audit
    args["capture"] = captureId
    args["x"] = x
    args["y"] = y
    func refuse(_ why: String) -> Result<[String: Any], CaptureError> {
        kernel.log("action_denied", ["class": action.classId, "target": target.id, "args": args, "reason": why,
                                     "caller": requester.audit])
        return .failure(CaptureError(why))
    }
    if case .text(let t) = action, let why = textRefusal(t) { return refuse(why) }
    guard let held = captureRetention.find(captureId) else {
        return refuse("no held screenshot \(captureId) (they are deleted after \(Int(heldCaptureSeconds))s); capture again")
    }
    guard held.target == target.id else { return refuse("screenshot \(captureId) is of \(held.target), not \(target.id)") }
    guard let point = globalPoint(x: x, y: y, in: held) else { return refuse("(\(x), \(y)) is outside screenshot \(captureId)") }

    let request = ActionRequest(classId: action.classId, target: target, args: args)
    let focus = FocusWatch.shared.generation
    let seq: Int, grantId: String
    switch kernel.authorize(request, requester: requester) {
    case .allow(let s, let g): (seq, grantId) = (s, g)
    case .deny(let why): return .failure(CaptureError(why))
    }
    func notStarted(_ why: String) -> Result<[String: Any], CaptureError> {
        kernel.recordOutcome(seq: seq, outcome: "not_started", fields: ["reason": why])
        return .failure(CaptureError(why))
    }
    guard let app = frontmostApplication(), app.bundleIdentifier == target.id else {
        return notStarted("the target is no longer frontmost")
    }
    guard let f = focusedElement() else { return notStarted("the focused element cannot be inspected") }
    guard f.pid == app.processIdentifier else { return notStarted("focus is not in \(target.label)") }
    guard let frame = f.frame, frame.contains(point) else {
        return notStarted("the focused element is not at the named point; click the field first")
    }
    if action.needsTextFocus {
        guard textEntryRoles.contains(f.role) else { return notStarted("the focused element (\(f.role)) is not a text field") }
        guard let window = f.window else { return notStarted("the focused field's window cannot be inspected") }
        let scan = scanSecureFields(roots: [window])
        guard scan.complete else { return notStarted(scan.problem ?? "could not check the window for password fields") }
        if !scan.rects.isEmpty { return notStarted("this window has a password field; it is treated as a credential prompt") }
    }
    if case .text = action, let why = labelSuggestsSecret(f.labels) { return notStarted(why) }
    if case .shortcut(let sc) = action, let letter = sc.letter, layoutCharacter(keyCode: sc.keyCode) != letter {
        return notStarted("the current keyboard layout does not put \(letter) on that key")
    }

    let element = f.element
    let check: () -> String? = {
        if FocusWatch.shared.generation != focus { return "another app was activated" }
        if let why = kernel.stillAllowed(request, grantId: grantId) { return why }
        guard let now = focusedElement(), CFEqual(now.element, element) else { return "focus moved to another element" }
        if case .shortcut(let sc) = action, let letter = sc.letter, layoutCharacter(keyCode: sc.keyCode) != letter {
            return "the keyboard layout changed"
        }
        return nil
    }
    let r = runSequence(keyboardSteps(action), check: check)
    let outcome = r.interrupted == nil ? "completed" : "interrupted"
    kernel.recordOutcome(seq: seq, outcome: outcome, fields: [
        "events_posted": r.completed, "steps": r.total, "reason": r.interrupted ?? NSNull(), "restored": r.restored])
    guard r.interrupted == nil else {
        return .failure(CaptureError("\(outcome) after \(r.completed) of \(r.total) steps: \(r.interrupted!)"))
    }
    return .success(["completed": r.completed, "total": r.total, "audit_seq": seq])
}
