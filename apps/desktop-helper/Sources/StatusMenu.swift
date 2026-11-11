// A menu-bar item, present for as long as this helper runs, so the user
// can always see that an agent holds desktop grants and take them away
// without the chat: revoke one grant, revoke all, or stop now (trips the
// kill switch and revokes everything). It works when Charon's UI is
// closed, because it belongs to the process that holds the grants.
//
// It also shows the redacted captures the helper still holds in memory
// (what the agent was shown), lets the user look at the latest one, and
// delete them all. This window and menu belong to this process, so they
// are never in a capture.

import AppKit
import Foundation

final class StatusMenu: NSObject {
    private let item: NSStatusItem
    private let kernel: Kernel
    private var refresh: Timer?
    private var viewer: NSWindow?

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
        let held = captureRetention.current()
        menu.addItem(withTitle: "Screenshots held in memory: \(held.count) (each deleted after \(Int(heldCaptureSeconds / 60)) min)",
                     action: nil, keyEquivalent: "")
        let show = NSMenuItem(title: "Show the latest screenshot", action: #selector(showLatest), keyEquivalent: "")
        show.target = self
        show.isEnabled = !held.isEmpty
        menu.addItem(show)
        let wipe = NSMenuItem(title: "Delete held screenshots", action: #selector(deleteCaptures), keyEquivalent: "")
        wipe.target = self
        wipe.isEnabled = !held.isEmpty
        menu.addItem(wipe)
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

    @objc private func showLatest() {
        guard let last = captureRetention.current().last, let image = NSImage(data: last.png) else { return }
        let view = NSImageView(image: image)
        view.imageScaling = .scaleProportionallyDown
        let size = NSSize(width: min(image.size.width, 900), height: min(image.size.height, 700))
        let w = viewer ?? NSWindow(contentRect: NSRect(origin: .zero, size: size),
                                   styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        w.isReleasedWhenClosed = false
        w.title = "What the agent saw: \(last.target), \(DateFormatter.localizedString(from: last.at, dateStyle: .none, timeStyle: .medium))"
        w.contentView = view
        w.setContentSize(size)
        w.center()
        viewer = w
        NSApp.activate(ignoringOtherApps: true)
        w.makeKeyAndOrderFront(nil)
    }

    @objc private func deleteCaptures() {
        _ = captureRetention.clear()
        viewer?.close()
        render(kernel.liveGrants())
    }

    @objc private func stopNow() {
        KillSwitch.shared.trip("stopped from the menu bar")
        _ = captureRetention.clear()
        viewer?.close()
        DispatchQueue.global().async { _ = self.kernel.revokeAll(by: "user (menu, stop)") }
    }
}
