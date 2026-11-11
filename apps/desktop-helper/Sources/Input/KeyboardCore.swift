// The parts of keyboard input that need no grant: what text may be typed,
// which shortcuts exist, and which fields look like they want a secret.
// Kept apart from Keyboard.swift so the tests can compile them.
//
// Nothing here can produce Return, Enter, paste, quit, close, or any other
// key that sends, submits or deletes beyond the focused text: those belong
// to desktop.input.destructive, which has no verb yet.

import CoreGraphics
import Foundation

/// Most characters one text action may type.
let maxTypedCharacters = 500

/// Why ``text`` may not be typed, or nil. Control characters are refused
/// outright: a newline is Return, which sends a chat message or submits a
/// form, and a tab moves focus.
func textRefusal(_ text: String) -> String? {
    if text.isEmpty { return "nothing to type" }
    if text.count > maxTypedCharacters { return "at most \(maxTypedCharacters) characters per action" }
    for scalar in text.unicodeScalars {
        switch scalar.properties.generalCategory {
        case .control, .lineSeparator, .paragraphSeparator, .privateUse, .surrogate, .unassigned:
            return "text may not contain control or line-break characters (U+\(String(scalar.value, radix: 16, uppercase: true)))"
        default: continue
        }
    }
    return nil
}

struct Shortcut {
    let name: String
    let keyCode: UInt16
    let flags: CGEventFlags
    /// For a letter shortcut, the character the key must produce in the
    /// current layout with Command held; checked before every event so a
    /// non-QWERTY layout cannot turn Cmd+A into Cmd+Q.
    let letter: Character?
    /// Edits text, so only while a text-entry field has focus.
    let needsTextFocus: Bool
}

/// The whole vocabulary. Not configurable. Deliberately absent: Return,
/// Enter, Space (presses a focused button), paste (the clipboard may hold
/// a password), cut, copy, redo, quit, close, save, print, and everything
/// with Command except the three below.
let shortcuts: [String: Shortcut] = {
    var s: [String: Shortcut] = [:]
    func add(_ name: String, _ code: UInt16, _ flags: CGEventFlags = [], letter: Character? = nil, text: Bool = false) {
        s[name] = Shortcut(name: name, keyCode: code, flags: flags, letter: letter, needsTextFocus: text)
    }
    let arrows: [(String, UInt16)] = [("left", 0x7B), ("right", 0x7C), ("down", 0x7D), ("up", 0x7E)]
    for (name, code) in arrows {
        add(name, code, .maskSecondaryFn)
        add("shift+" + name, code, [.maskShift, .maskSecondaryFn])
    }
    add("home", 0x73); add("end", 0x77); add("page_up", 0x74); add("page_down", 0x79)
    add("escape", 0x35)
    add("tab", 0x30); add("shift+tab", 0x30, .maskShift)
    add("backspace", 0x33, text: true)
    add("forward_delete", 0x75, text: true)
    add("select_all", 0x00, .maskCommand, letter: "a", text: true)
    add("undo", 0x06, .maskCommand, letter: "z", text: true)
    add("find", 0x03, .maskCommand, letter: "f")
    return s
}()

let textEntryRoles: Set<String> = ["AXTextField", "AXTextArea", "AXComboBox"]

/// A heuristic, English only: does a field's visible label suggest it
/// wants a secret even though it is not a secure field (one-time codes,
/// card numbers, PINs)? Typing into such a field is refused. It misses
/// anything labelled otherwise; the structural checks (secure field, secure
/// input, a password field anywhere in the window) do not depend on it.
func labelSuggestsSecret(_ labels: [String]) -> String? {
    let patterns = ["password", "passcode", "passphrase", "one-time", "one time", "otp", "2fa", "two-factor",
                    "verification code", "security code", "auth code", "authentication code", "cvv", "cvc",
                    "card number", "credit card", "pin", "secret", "recovery", "seed phrase", "api key", "token"]
    for label in labels {
        let words = label.lowercased()
        for p in patterns {
            let hit = p.count <= 4
                ? words.range(of: "\\b\(NSRegularExpression.escapedPattern(for: p))\\b", options: .regularExpression) != nil
                : words.contains(p)
            if hit { return "the field is labelled \"\(String(label.prefix(60)))\", which suggests a secret" }
        }
    }
    return nil
}
