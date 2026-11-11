// The single mediation point: every action of every registered family is
// decided here, and every decision is written to the audit before it takes
// effect.
//
// Rules, all enforced on this side of the channel so a buggy or hostile
// caller cannot widen them, and none read from config, flags or env:
//
// * Default deny. An action needs a live grant for its exact class and
//   target. Nothing is implied by any other grant.
// * Only the user grants. ``requestGrant`` shows the user a prompt owned by
//   this process; the caller can ask, narrow (shorten) or revoke, never
//   grant or extend. An unanswered prompt is a denial.
// * Grants live in this process's memory only: (class, target, expiry).
//   They die on expiry, on revocation, when the agent stops (the channel
//   closes and the process exits) and on any restart.
// * A grant lasts at most ``maxGrantSeconds``.
// * Destructive classes need a fresh approval for every single action,
//   even inside a live grant.
// * Never-approvable guards registered by the family run before anything
//   else; no grant or approval overrides them.
// * Each class has a hard per-minute rate limit.
// * If the audit cannot be written, the action is denied.

import Foundation
import Security

enum Decision {
    case allow(seq: Int, grantId: String)
    case deny(String)
}

struct Grant {
    let id: String
    let classId: String
    let target: Target
    let grantedAt: Date
    var expiresAt: Date
    var terms: [String: String] = [:]

    func json(now: Date) -> [String: Any] {
        ["id": id, "class": classId, "target": target.id, "target_label": target.label,
         "granted_at": ISO8601DateFormatter().string(from: grantedAt),
         "expires_at": ISO8601DateFormatter().string(from: expiresAt),
         "seconds_left": max(0, Int(expiresAt.timeIntervalSince(now))), "terms": terms]
    }
}

struct Prompt {
    let title: String
    let lines: [String]
    let allowLabel: String
}

enum PromptAnswer {
    case allow
    case deny(String)     // "user", "timeout", "unavailable: ..."
}

/// Asks the human. The implementation must be owned by this process and
/// must resolve to deny when nobody answers.
protocol Prompter {
    func ask(_ prompt: Prompt) -> PromptAnswer
}

final class Kernel {
    static let maxGrantSeconds: TimeInterval = 3600
    static let minGrantSeconds: TimeInterval = 60

    let audit: AuditLog
    let prompter: Prompter
    let now: () -> Date
    /// Called (outside the lock) whenever the set of live grants changes,
    /// so the helper's menu can show what is granted.
    var onGrantsChanged: (([Grant]) -> Void)?

    private let lock = NSLock()
    private var classes: [String: ActionClass] = [:]
    private var guards: [String: [NeverApprovable]] = [:]
    private var grants: [String: Grant] = [:]
    private var recent: [String: [Date]] = [:]

    init(audit: AuditLog, prompter: Prompter, now: @escaping () -> Date = Date.init) {
        self.audit = audit
        self.prompter = prompter
        self.now = now
    }

    func register(family: String, classes newClasses: [ActionClass], guards newGuards: [NeverApprovable]) {
        lock.lock(); defer { lock.unlock() }
        for c in newClasses where c.family == family { classes[c.id] = c }
        guards[family, default: []].append(contentsOf: newGuards)
    }

    func actionClass(_ id: String) -> ActionClass? {
        lock.lock(); defer { lock.unlock() }
        return classes[id]
    }

    // ── Grants ──────────────────────────────────────────────────────

    /// Ask the user for a grant. Returns the grant, or the denial reason.
    func requestGrant(classId: String, target: Target, seconds: Double, reason: String,
                      terms: [String: String] = [:], requester: Requester) -> Result<Grant, KernelDenial> {
        guard let cls = actionClass(classId) else {
            log("grant_refused", ["class": classId, "target": target.id, "reason": "unknown action class",
                                  "caller": requester.audit])
            return .failure(.init("unknown action class \(classId)"))
        }
        let probe = ActionRequest(classId: classId, target: target, args: [:], terms: terms)
        if let why = refusal(cls, probe, atGrant: true) {
            log("grant_refused", ["class": classId, "target": target.id, "reason": why, "terms": terms,
                                  "caller": requester.audit])
            return .failure(.init(why))
        }
        let duration = min(max(seconds.isFinite ? seconds : 0, Kernel.minGrantSeconds), Kernel.maxGrantSeconds)
        let minutes = Int((duration / 60).rounded())
        do {
            try audit.append("grant_requested", ["class": classId, "target": target.id, "seconds": Int(duration),
                                                 "reason": String(reason.prefix(300)), "terms": terms,
                                                 "caller": requester.audit])
        } catch { return .failure(.init("\(error)")) }
        var lines = [
            "\(requester.description) asks to:",
            "  \(cls.summary)",
            "in: \(target.label) (\(target.id))",
            "for: \(minutes) minute\(minutes == 1 ? "" : "s"), then it expires.",
        ]
        if let caveat = cls.caveat { lines.append(caveat) }
        if cls.destructive { lines.append("Each destructive action will still ask you separately.") }
        if !terms.isEmpty {
            lines.append("Stated by the requester (not checked by Charon Desktop):")
            for k in terms.keys.sorted() { lines.append("  \(k): \(String(terms[k]!.prefix(120)))") }
        }
        if !reason.isEmpty { lines.append("Agent's stated reason (unverified): \"\(String(reason.prefix(200)))\"") }
        let answer = prompter.ask(Prompt(title: "Allow Charon to act on your Mac?", lines: lines,
                                         allowLabel: "Allow for \(minutes) min"))
        guard case .allow = answer else {
            let why: String
            if case .deny(let r) = answer { why = r } else { why = "denied" }
            log("grant_denied", ["class": classId, "target": target.id, "by": why])
            return .failure(.init("not granted (\(why))"))
        }
        let t = now()
        let grant = Grant(id: randomId(), classId: classId, target: target, grantedAt: t,
                          expiresAt: t.addingTimeInterval(duration), terms: terms)
        do {
            try audit.append("grant_issued", ["grant": grant.json(now: t), "by": "user"])
        } catch { return .failure(.init("\(error)")) }
        lock.lock(); grants[grant.id] = grant; lock.unlock()
        changed()
        return .success(grant)
    }

    /// Shorten a grant. A later expiry than the current one is refused:
    /// the caller can narrow, never extend.
    func narrow(id: String, seconds: Double, requester: Requester) -> Result<Grant, KernelDenial> {
        lock.lock()
        guard var g = grants[id] else { lock.unlock(); return .failure(.init("no such grant")) }
        let newExpiry = now().addingTimeInterval(max(0, seconds))
        guard newExpiry < g.expiresAt else { lock.unlock(); return .failure(.init("a grant can only be shortened")) }
        g.expiresAt = newExpiry
        grants[id] = g
        lock.unlock()
        log("grant_narrowed", ["grant": id, "expires_at": ISO8601DateFormatter().string(from: newExpiry),
                               "caller": requester.audit])
        changed()
        return .success(g)
    }

    func revoke(id: String, by: String) -> Bool {
        lock.lock(); let g = grants.removeValue(forKey: id); lock.unlock()
        guard g != nil else { return false }
        log("grant_revoked", ["grant": id, "by": by])
        changed()
        return true
    }

    func revokeAll(by: String) -> Int {
        lock.lock(); let ids = Array(grants.keys); grants.removeAll(); lock.unlock()
        if !ids.isEmpty {
            log("grants_revoked_all", ["grants": ids, "by": by])
            changed()
        }
        return ids.count
    }

    func liveGrants() -> [Grant] {
        expire()
        lock.lock(); defer { lock.unlock() }
        return grants.values.sorted { $0.expiresAt < $1.expiresAt }
    }

    private func expire() {
        let t = now()
        lock.lock()
        let dead = grants.values.filter { $0.expiresAt <= t }
        for g in dead { grants.removeValue(forKey: g.id) }
        lock.unlock()
        for g in dead { log("grant_expired", ["grant": g.id, "class": g.classId, "target": g.target.id]) }
        if !dead.isEmpty { changed() }
    }

    private func changed() {
        let live: [Grant]
        lock.lock(); live = grants.values.sorted { $0.expiresAt < $1.expiresAt }; lock.unlock()
        onGrantsChanged?(live)
    }

    // ── Decisions ───────────────────────────────────────────────────

    private func refusal(_ cls: ActionClass, _ request: ActionRequest, atGrant: Bool = false) -> String? {
        lock.lock(); let gs = guards[cls.family] ?? []; lock.unlock()
        for g in gs where !atGrant || g.appliesAtGrant {
            if let why = g.refusal(for: request) { return why }
        }
        return nil
    }

    /// Decide one action. On allow, the authorisation is already in the
    /// audit; the verb then acts and calls ``recordOutcome`` with the seq.
    func authorize(_ request: ActionRequest, requester: Requester) -> Decision {
        expire()
        guard let cls = actionClass(request.classId) else {
            return deny(request, "unknown action class \(request.classId)", requester)
        }
        if let why = refusal(cls, request) {
            return deny(request, "never allowed: \(why)", requester)
        }
        let t = now()
        lock.lock()
        let grant = grants.values.first {
            $0.classId == cls.id && $0.target == request.target && $0.terms == request.terms && $0.expiresAt > t
        }
        var window = (recent[cls.id] ?? []).filter { t.timeIntervalSince($0) < 60 }
        let limited = window.count >= cls.maxPerMinute
        lock.unlock()
        guard let g = grant else {
            return deny(request, "no live grant for \(cls.id) in \(request.target.id) under these terms", requester)
        }
        if limited {
            return deny(request, "rate limit: \(cls.maxPerMinute) per minute for \(cls.id)", requester)
        }
        if cls.destructive {
            let answer = prompter.ask(Prompt(
                title: "Allow this one action?",
                lines: ["\(requester.description) wants to do something that may not be undoable:",
                        "  \(cls.summary)", "in: \(request.target.label) (\(request.target.id))",
                        "details: \(summarize(request.args))"],
                allowLabel: "Allow once"))
            guard case .allow = answer else {
                let why: String
                if case .deny(let r) = answer { why = r } else { why = "denied" }
                return deny(request, "destructive action not approved (\(why))", requester)
            }
        }
        do {
            let seq = try audit.append("action_authorized", [
                "class": cls.id, "target": request.target.id, "args": request.args, "terms": request.terms,
                "grant": g.id,
                "per_action_approval": cls.destructive, "caller": requester.audit])
            lock.lock()
            window.append(t)
            recent[cls.id] = window
            lock.unlock()
            return .allow(seq: seq, grantId: g.id)
        } catch {
            return .deny("audit unavailable, not acting: \(error)")
        }
    }

    /// Re-check an authorised action after it has happened but before its
    /// result is used: the never-approvable guards again, and that the
    /// grant it ran under is still live. Returns the reason to discard the
    /// result, or nil. Consumes no rate budget and writes nothing; the verb
    /// records the outcome. This closes the window between the check in
    /// ``authorize`` and the act, for verbs whose result can be thrown away
    /// (a capture taken after focus moved is discarded, not delivered).
    func stillAllowed(_ request: ActionRequest, grantId: String) -> String? {
        expire()
        guard let cls = actionClass(request.classId) else { return "unknown action class \(request.classId)" }
        if let why = refusal(cls, request) { return why }
        lock.lock(); let g = grants[grantId]; lock.unlock()
        guard let grant = g, grant.classId == cls.id, grant.target == request.target, grant.terms == request.terms,
              grant.expiresAt > now() else {
            return "the grant ended while acting"
        }
        return nil
    }

    func recordOutcome(seq: Int, outcome: String, fields: [String: Any] = [:]) {
        var f = fields
        f["authorized_seq"] = seq
        f["outcome"] = outcome
        log("action_outcome", f)
    }

    /// Record an event that is not a decision (kill switch trips, start,
    /// stop). Best effort: losing it must not block a stop.
    func log(_ kind: String, _ fields: [String: Any]) {
        _ = try? audit.append(kind, fields)
    }

    private func deny(_ r: ActionRequest, _ why: String, _ requester: Requester) -> Decision {
        log("action_denied", ["class": r.classId, "target": r.target.id, "args": r.args, "terms": r.terms,
                              "reason": why,
                              "caller": requester.audit])
        return .deny(why)
    }

    private func summarize(_ args: [String: Any]) -> String {
        guard let d = try? JSONSerialization.data(withJSONObject: args, options: [.sortedKeys]) else { return "" }
        return String(String(decoding: d, as: UTF8.self).prefix(200))
    }

    private func randomId() -> String {
        var b = [UInt8](repeating: 0, count: 8)
        _ = SecRandomCopyBytes(kSecRandomDefault, b.count, &b)
        return "g-" + b.map { String(format: "%02x", $0) }.joined()
    }
}

struct KernelDenial: Error, CustomStringConvertible {
    let reason: String
    init(_ reason: String) { self.reason = reason }
    var description: String { reason }
}
