// Desktop control, the first family registered with the kernel: its
// action classes and the refusals no approval can override.
//
// Never approvable (compiled in, not configurable):
// * Any app on the blocklist below: password managers, Keychain Access,
//   the system's credential and authentication prompts, System Settings
//   (where privacy grants, including this helper's own, are changed),
//   this helper itself (its approval dialog must never be clicked by the
//   agent), and terminals (typing into one is running shell commands,
//   which this helper must never become a route to).
// * Secure event input on anywhere (macOS turns it on while a password
//   field has focus; it is also what keeps keystrokes from other apps).
// * A focused element that is a secure text field (AXSecureTextField),
//   checked by Accessibility. If the focused element cannot be inspected,
//   that is a refusal too.
// * A frontmost app that cannot be identified, or that is not the target
//   the action was granted for.
// * Input or capture while the kill switch is not armed. Armed means the
//   user was idle for ``minimumIdleSeconds`` and has not touched keyboard or
//   mouse since: one definition of idle, shared by input and capture.
// * Capture without a stated local destination (DestinationGuard). The
//   destination is the requester's claim; the guard makes the claim
//   explicit and on record, it cannot make it true.
// Capture is refused under the same conditions as input, so the screen
// is never recorded while a secure field has focus.

import AppKit
import ApplicationServices
import Carbon
import Foundation

enum Desktop {
    static let family = "desktop"
    static let observeScreen = "desktop.observe.screen"
    static let pointer = "desktop.input.pointer"
    static let text = "desktop.input.text"
    static let shortcut = "desktop.input.shortcut"
    static let destructive = "desktop.input.destructive"

    static let classes: [ActionClass] = [
        ActionClass(id: observeScreen, family: family, summary: "see this app's windows (screenshots)",
                    destructive: false, maxPerMinute: 30,
                    caveat: "Only password fields are hidden. Anything else on screen in this app, such as codes, card numbers or messages, will be in the screenshots."),
        ActionClass(id: pointer, family: family, summary: "move the pointer, click and scroll",
                    destructive: false, maxPerMinute: 120),
        ActionClass(id: text, family: family, summary: "type text (never into password fields)",
                    destructive: false, maxPerMinute: 60),
        ActionClass(id: shortcut, family: family, summary: "press keyboard shortcuts",
                    destructive: false, maxPerMinute: 60),
        ActionClass(id: destructive, family: family,
                    summary: "do things that may not be undoable (quit, close, delete, send)",
                    destructive: true, maxPerMinute: 20,
                    unavailable: "destructive actions are not available yet: Charon Desktop has no verb for them, so there is nothing to grant"),
    ]

    static let inputClasses: Set<String> = [pointer, text, shortcut, destructive]
    /// Classes that run only while the kill switch is armed (user idle).
    static let armedClasses: Set<String> = inputClasses.union([observeScreen])
    /// The only destination kind a capture may state.
    static let localDestination = "local-loopback"

    /// Bundle identifiers the agent may never act in or capture.
    static let blocklist: [String: String] = [
        "ai.charon.desktop": "Charon Desktop itself",
        "com.apple.keychainaccess": "Keychain Access",
        "com.apple.Passwords": "Passwords",
        "com.apple.SecurityAgent": "a system authentication prompt",
        "com.apple.LocalAuthentication.UIAgent": "a Touch ID / password prompt",
        "com.apple.coreautha": "a system authentication prompt",
        "com.apple.loginwindow": "the login window",
        "com.apple.systempreferences": "System Settings",
        "com.apple.Settings": "System Settings",
        "com.1password.1password": "1Password",
        "com.agilebits.onepassword7": "1Password",
        "com.agilebits.onepassword-osx": "1Password",
        "com.bitwarden.desktop": "Bitwarden",
        "com.lastpass.LastPass": "LastPass",
        "com.dashlane.dashlanephonefinal": "Dashlane",
        "org.keepassxc.keepassxc": "KeePassXC",
        "in.sinew.Enpass-Desktop": "Enpass",
        "com.nordpass.macos.NordPass": "NordPass",
        "me.proton.pass.electron": "Proton Pass",
        "com.apple.Terminal": "Terminal",
        "com.googlecode.iterm2": "iTerm",
        "com.mitchellh.ghostty": "Ghostty",
        "org.alacritty": "Alacritty",
        "net.kovidgoyal.kitty": "kitty",
        "com.github.wez.wezterm": "WezTerm",
        "dev.warp.Warp-Stable": "Warp",
    ]

    static func register(with kernel: Kernel) {
        kernel.register(family: family, classes: classes, guards: [
            BlocklistGuard(), DestinationGuard(), KillSwitchGuard(), SecureInputGuard(), FrontAppGuard(),
            SecureFieldGuard(),
        ])
    }

    /// Resolve a bundle id to an installed app, or nil (refused upstream).
    static func target(bundleId: String) -> Target? {
        guard !bundleId.isEmpty,
              let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleId) else { return nil }
        return Target(id: bundleId, label: FileManager.default.displayName(atPath: url.path))
    }
}

struct BlocklistGuard: NeverApprovable {
    var appliesAtGrant: Bool { true }
    func refusal(for r: ActionRequest) -> String? {
        Desktop.blocklist[r.target.id].map { "\($0) is on the never-allowed list" }
    }
}

struct KillSwitchGuard: NeverApprovable {
    var appliesAtGrant: Bool { false }
    func refusal(for r: ActionRequest) -> String? {
        guard Desktop.armedClasses.contains(r.classId), KillSwitch.shared.tripped else { return nil }
        return "kill switch not armed (\(KillSwitch.shared.reason))"
    }
}

/// A capture must state that it goes to a local model. This is a recorded
/// claim, not a boundary: once the caller holds the image the helper cannot
/// see where it goes. The caller (charon.computer.destination) decides the
/// claim from the endpoint its request will actually reach.
struct DestinationGuard: NeverApprovable {
    var appliesAtGrant: Bool { true }
    func refusal(for r: ActionRequest) -> String? {
        guard r.classId == Desktop.observeScreen else { return nil }
        guard (r.terms["destination"] ?? "").isEmpty == false else {
            return "a screenshot request must state where it will be sent"
        }
        guard r.terms["destination_kind"] == Desktop.localDestination else {
            return "screenshots may only go to a local model (stated: \(r.terms["destination_kind"] ?? "none"))"
        }
        return nil
    }
}

struct SecureInputGuard: NeverApprovable {
    var appliesAtGrant: Bool { false }
    func refusal(for r: ActionRequest) -> String? {
        sessionBlocker()
    }
}

struct FrontAppGuard: NeverApprovable {
    var appliesAtGrant: Bool { false }
    func refusal(for r: ActionRequest) -> String? {
        guard let app = frontmostApplication(), let id = app.bundleIdentifier else {
            return "the frontmost app cannot be identified"
        }
        if let name = Desktop.blocklist[id] { return "\(name) is frontmost" }
        if id != r.target.id { return "\(app.localizedName ?? id) is frontmost, not \(r.target.label)" }
        return nil
    }
}

struct SecureFieldGuard: NeverApprovable {
    var appliesAtGrant: Bool { false }
    func refusal(for r: ActionRequest) -> String? {
        switch focusedElementIsSecure() {
        case .some(false): return nil
        case .some(true): return "a password field has focus"
        case .none: return "the focused element cannot be inspected (Accessibility not granted or app not responding)"
        }
    }
}

/// true: a secure text field has focus; false: something else (or
/// nothing) has focus; nil: cannot tell, which callers refuse.
func focusedElementIsSecure() -> Bool? {
    guard AXIsProcessTrusted() else { return nil }
    let system = AXUIElementCreateSystemWide()
    AXUIElementSetMessagingTimeout(system, 1.0)
    var focused: CFTypeRef?
    let r = AXUIElementCopyAttributeValue(system, kAXFocusedUIElementAttribute as CFString, &focused)
    if r == .noValue { return false }
    guard r == .success, let value = focused, CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    let element = value as! AXUIElement
    var role: CFTypeRef?
    var subrole: CFTypeRef?
    AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &role)
    AXUIElementCopyAttributeValue(element, kAXSubroleAttribute as CFString, &subrole)
    let secure = kAXSecureTextFieldSubrole as String
    return (subrole as? String) == secure || (role as? String) == secure
}
