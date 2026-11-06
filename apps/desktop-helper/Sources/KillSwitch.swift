// The kill switch: the agent acts only while the user is away from the
// keyboard and mouse, and the first human input stops it mid-sequence.
//
// Why here: the tap and the injection loop live in this one process so a
// human keystroke cuts an injected sequence between two events, without a
// round trip through Python.
//
// Mechanism:
// * A listen-only CGEventTap (it can observe, never modify or swallow
//   input) runs on its own thread with its own run loop. It needs the
//   Input Monitoring grant; without it the switch cannot arm, and nothing
//   may be injected (fail closed).
// * Every event this helper posts carries a random 64-bit tag, drawn per
//   run and held only in this process's memory, in the event-source user
//   data field. The tap ignores events with that tag; anything else — a
//   human, or another program's synthetic input — trips the switch. A
//   foreign process cannot forge the tag without reading our memory, which
//   the hardened runtime denies to same-user processes.
// * If macOS disables the tap (it does so when a tap is slow, or on secure
//   input), that also trips the switch: we can no longer see the human.
// * Tripping sets one atomic flag. The sequencer checks it before posting
//   each single event and while waiting between events, so a sequence is
//   cut after at most the event in flight.
// * On a trip the sequencer restores the frontmost app and the pointer
//   position it recorded when the sequence started, then reports.
// * Acting also requires an active, unlocked console session and no
//   secure event input (a password field focused anywhere turns it on).
//
// The switch never re-arms itself. A trip latches until the user has been
// idle for at least ``minimumIdleSeconds`` and the caller explicitly
// re-arms; that floor is a constant here, not a setting.

import AppKit
import ApplicationServices
import Carbon
import CoreGraphics
import Foundation

let minimumIdleSeconds: Double = 30

final class KillSwitch {
    static let shared = KillSwitch()

    let tag: Int64 = {
        var v: Int64 = 0
        while v == 0 { _ = SecRandomCopyBytes(kSecRandomDefault, MemoryLayout<Int64>.size, &v) }
        return v
    }()

    private let lock = NSLock()
    private var _tripped = true            // latched until armed
    private var _reason = "not armed"
    private var _lastHumanEvent = Date.distantPast
    private var _tapAlive = false
    private var _lastSelfSeen = Date.distantPast
    private var tap: CFMachPort?
    var onTrip: ((String) -> Void)?

    var tripped: Bool { lock.lock(); defer { lock.unlock() }; return _tripped }
    var reason: String { lock.lock(); defer { lock.unlock() }; return _reason }
    var tapAlive: Bool { lock.lock(); defer { lock.unlock() }; return _tapAlive }
    var secondsSinceHuman: Double { lock.lock(); defer { lock.unlock() }; return Date().timeIntervalSince(_lastHumanEvent) }

    func trip(_ why: String) {
        lock.lock()
        let first = !_tripped
        _tripped = true
        _reason = why
        lock.unlock()
        if first { onTrip?(why) }
    }

    private func sawHuman(_ kind: String) {
        lock.lock(); _lastHumanEvent = Date(); lock.unlock()
        trip("human input: \(kind)")
    }

    /// Start the tap thread. Returns false (and stays tripped) when the
    /// tap cannot be created, which means Input Monitoring is not granted.
    func start() -> Bool {
        let types: [CGEventType] = [.keyDown, .keyUp, .flagsChanged, .leftMouseDown, .leftMouseUp,
                                    .rightMouseDown, .rightMouseUp, .otherMouseDown, .otherMouseUp,
                                    .mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged,
                                    .scrollWheel]
        let mask = types.reduce(CGEventMask(0)) { $0 | (CGEventMask(1) << $1.rawValue) }
        let ready = DispatchSemaphore(value: 0)
        var created = false
        let thread = Thread { [self] in
            let callback: CGEventTapCallBack = { _, type, event, _ in
                let ks = KillSwitch.shared
                if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                    ks.lock.lock(); ks._tapAlive = false; ks.lock.unlock()
                    ks.trip("event tap disabled by macOS")
                    if let t = ks.tap { CGEvent.tapEnable(tap: t, enable: true) }
                    ks.lock.lock(); ks._tapAlive = ks.tap.map { CGEvent.tapIsEnabled(tap: $0) } ?? false; ks.lock.unlock()
                    return Unmanaged.passUnretained(event)
                }
                if event.getIntegerValueField(.eventSourceUserData) != ks.tag {
                    ks.sawHuman(String(describing: type))
                } else {
                    ks.lock.lock(); ks._lastSelfSeen = Date(); ks.lock.unlock()
                }
                return Unmanaged.passUnretained(event)
            }
            guard let port = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap,
                                               options: .listenOnly, eventsOfInterest: mask,
                                               callback: callback, userInfo: nil) else {
                ready.signal()
                return
            }
            self.tap = port
            let source = CFMachPortCreateRunLoopSource(nil, port, 0)
            CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
            CGEvent.tapEnable(tap: port, enable: true)
            self.lock.lock(); self._tapAlive = true; self.lock.unlock()
            created = true
            ready.signal()
            CFRunLoopRun()
        }
        thread.name = "kill-switch-tap"
        thread.qualityOfService = .userInteractive
        thread.start()
        ready.wait()
        return created
    }

    /// Re-arm after a trip. Refused unless both grants are present, the
    /// tap demonstrably sees posted input, the session can be acted in,
    /// and the user has been idle long enough.
    ///
    /// A created tap is not proof: without Input Monitoring macOS creates
    /// it and then silently withholds events, and without Accessibility
    /// posted events are silently dropped. So arming posts one tagged,
    /// motionless mouse event and requires the tap to see it come back.
    func arm() -> (Bool, String) {
        if !CGPreflightListenEventAccess() { return (false, "Input Monitoring is not granted; the switch cannot see you") }
        if !CGPreflightPostEventAccess() { return (false, "Accessibility is not granted") }
        if !tapAlive { return (false, "kill switch unavailable: event tap not running") }
        if let why = sessionBlocker() { return (false, why) }
        if !roundTrip() { return (false, "kill switch unverified: a posted probe never reached the event tap") }
        let idle = secondsSinceHuman
        if idle < minimumIdleSeconds {
            return (false, "user active \(Int(idle))s ago; need \(Int(minimumIdleSeconds))s idle")
        }
        lock.lock(); _tripped = false; _reason = ""; lock.unlock()
        return (true, "armed")
    }

    /// Post one tagged mouse-moved event at the pointer's position and wait
    /// up to 500 ms for the tap to report it.
    private func roundTrip() -> Bool {
        let t0 = Date()
        let at = CGEvent(source: nil)?.location ?? .zero
        guard let e = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: at, mouseButton: .left) else { return false }
        e.setIntegerValueField(.eventSourceUserData, value: tag)
        e.post(tap: .cghidEventTap)
        while Date().timeIntervalSince(t0) < 0.5 {
            lock.lock(); let seen = _lastSelfSeen > t0; lock.unlock()
            if seen { return true }
            usleep(5_000)
        }
        return false
    }

    func status() -> [String: Any] {
        let idle = secondsSinceHuman
        return ["armed": !tripped, "reason": reason, "tap_alive": tapAlive,
                "input_monitoring": CGPreflightListenEventAccess(), "accessibility": CGPreflightPostEventAccess(),
                "idle_seconds": idle.isFinite && idle < 1e9 ? Int(idle) : NSNull(),
                "minimum_idle_seconds": Int(minimumIdleSeconds),
                "session_blocker": sessionBlocker() ?? NSNull()]
    }
}

/// Why the session cannot be acted in right now, or nil if it can.
func sessionBlocker() -> String? {
    guard let dict = CGSessionCopyCurrentDictionary() as? [String: Any] else { return "no login session" }
    if (dict[kCGSessionOnConsoleKey as String] as? Bool) != true { return "session is not on the console" }
    if (dict["CGSSessionScreenIsLocked"] as? Bool) == true { return "screen is locked" }
    if IsSecureEventInputEnabled() { return "secure input is on (a password field has focus somewhere)" }
    return nil
}

// ── Sequencer ───────────────────────────────────────────────────────

/// One step of an injected sequence. Built only by the helper's own typed
/// verbs; there is no way to send raw steps over the channel.
enum Step {
    case post(CGEvent)
    case wait(Double)
}

struct SequenceResult {
    let completed: Int
    let total: Int
    let interrupted: String?
    let restored: Bool
}

/// Runs a sequence, checking the switch before each event and while
/// waiting. On interruption restores the front app and pointer.
func runSequence(_ steps: [Step]) -> SequenceResult {
    let ks = KillSwitch.shared
    if ks.tripped { return SequenceResult(completed: 0, total: steps.count, interrupted: ks.reason, restored: false) }
    let frontApp = frontmostApplication()
    let pointer = CGEvent(source: nil)?.location
    var done = 0
    for step in steps {
        if let why = sessionBlocker() { ks.trip(why) }
        if ks.tripped { break }
        switch step {
        case .post(let event):
            event.setIntegerValueField(.eventSourceUserData, value: ks.tag)
            event.post(tap: .cghidEventTap)
        case .wait(let seconds):
            let until = Date().addingTimeInterval(seconds)
            while Date() < until && !ks.tripped { usleep(5_000) }
        }
        done += 1
    }
    guard ks.tripped else { return SequenceResult(completed: done, total: steps.count, interrupted: nil, restored: false) }
    if let p = pointer { CGWarpMouseCursorPosition(p) }
    if let app = frontApp, !app.isTerminated { DispatchQueue.main.sync { _ = app.activate() } }
    return SequenceResult(completed: done, total: steps.count, interrupted: ks.reason, restored: true)
}

/// The app owning the frontmost normal window, read from the window
/// server rather than NSWorkspace (whose cached value goes stale in a
/// process without a running main loop). Nil when it cannot be told,
/// which callers treat as "deny".
func frontmostApplication() -> NSRunningApplication? {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] else { return nil }
    for w in list where (w[kCGWindowLayer as String] as? Int) == 0 {
        if let pid = w[kCGWindowOwnerPID as String] as? pid_t, let app = NSRunningApplication(processIdentifier: pid) {
            return app
        }
    }
    return nil
}

/// A harmless end-to-end check of the switch: posts tagged mouse-moved
/// events to the pointer's current position (the pointer does not move)
/// for ``seconds``. Our own events must not trip it; moving the mouse or
/// pressing a key must cut it mid-way.
func killSwitchSelfTest(seconds: Double) -> SequenceResult {
    var steps: [Step] = []
    let n = max(1, Int(seconds / 0.05))
    for _ in 0..<n {
        let at = CGEvent(source: nil)?.location ?? .zero
        if let e = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: at, mouseButton: .left) {
            steps.append(.post(e))
        }
        steps.append(.wait(0.05))
    }
    return runSequence(steps)
}
