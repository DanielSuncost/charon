// A menu-bar item, present for as long as this helper runs, so the user
// can always see that an agent holds desktop grants and take them away
// without the chat: revoke one grant, revoke all, or stop now (trips the
// kill switch and revokes everything). It works when Charon's UI is
// closed, because it belongs to the process that holds the grants.

import AppKit
import Foundation

final class StatusMenu: NSObject {
    private let item: NSStatusItem
    private let kernel: Kernel
    private var refresh: Timer?

    init(kernel: Kernel) {
        self.kernel = kernel
        self.item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        super.init()
        let t = Timer(timeInterval: 30, repeats: true) { [weak self] _ in self?.render(kernel.liveGrants()) }
        RunLoop.main.add(t, forMode: .common)
        refresh = t
        render([])
    }

    /// Safe to call from any thread.
    func update(_ grants: [Grant]) {
        DispatchQueue.main.async { self.render(grants) }
    }

    private func render(_ grants: [Grant]) {
        let armed = !KillSwitch.shared.tripped
        item.button?.title = grants.isEmpty ? "Charon ◦" : "Charon ● \(grants.count)"
        item.button?.toolTip = grants.isEmpty ? "Charon Desktop: nothing granted" : "Charon Desktop: \(grants.count) live grant(s)"
        let menu = NSMenu()
        menu.addItem(withTitle: grants.isEmpty ? "No live grants" : "Live grants (click to revoke):", action: nil, keyEquivalent: "")
        let now = Date()
        for g in grants {
            let mins = max(0, Int(g.expiresAt.timeIntervalSince(now) / 60))
            let summary = kernel.actionClass(g.classId)?.summary ?? g.classId
            let entry = NSMenuItem(title: "  \(summary) — \(g.target.label) · \(mins) min left",
                                   action: #selector(revokeOne(_:)), keyEquivalent: "")
            entry.target = self
            entry.representedObject = g.id
            menu.addItem(entry)
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: armed ? "Kill switch: armed (touch any key to pause)" : "Kill switch: paused",
                     action: nil, keyEquivalent: "")
        let all = NSMenuItem(title: "Revoke all grants", action: #selector(revokeAll), keyEquivalent: "")
        all.target = self
        all.isEnabled = !grants.isEmpty
        menu.addItem(all)
        let stop = NSMenuItem(title: "Stop now", action: #selector(stopNow), keyEquivalent: "")
        stop.target = self
        menu.addItem(stop)
        item.menu = menu
    }

    @objc private func revokeOne(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        DispatchQueue.global().async { _ = self.kernel.revoke(id: id, by: "user (menu)") }
    }

    @objc private func revokeAll() {
        DispatchQueue.global().async { _ = self.kernel.revokeAll(by: "user (menu)") }
    }

    @objc private func stopNow() {
        KillSwitch.shared.trip("stopped from the menu bar")
        DispatchQueue.global().async { _ = self.kernel.revokeAll(by: "user (menu, stop)") }
    }
}
