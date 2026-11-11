// The supervision kernel's vocabulary. Nothing in Kernel/ knows about the
// desktop: a *family* (desktop control is the first) registers its action
// classes and its never-approvable guards, and every action of that family
// is decided by the same grant, approval and audit machinery.
//
// THE SEAM. Gating something else — a software team's tools, an overseer's
// own dispatch — means registering another family here and running this
// kernel in a process that holds what that family needs. Its entry points
// are ``Kernel.requestGrant`` (ask the user), ``Kernel.authorize`` (decide
// one action) and ``Kernel.recordOutcome`` (what happened), and the audit
// it writes (Audit.swift) is the evidence a supervisor reads instead of a
// worker's self-report.

import Foundation

/// A kind of action a family can ask to perform. Grants name a class.
struct ActionClass {
    let id: String            // "desktop.input.text"
    let family: String        // "desktop"
    let summary: String       // plain words for the approval dialog
    /// Irreversible or destructive: every single action needs its own
    /// approval, even inside a live grant.
    let destructive: Bool
    /// Hard cap on authorised actions of this class per rolling minute.
    let maxPerMinute: Int
    /// One plain line the grant dialog always shows: what this class does
    /// not protect against, so the user approves knowing it.
    var caveat: String? = nil
}

/// What an action acts on. For the desktop family the id is the target
/// app's bundle identifier.
struct Target: Hashable {
    let id: String
    let label: String
}

/// One action, as the family's verb describes it to the kernel. ``args``
/// is what the audit records, so the verb must pass a redacted form (never
/// typed text verbatim when it could be sensitive).
struct ActionRequest {
    let classId: String
    let target: Target
    let args: [String: Any]
    /// Conditions the caller states and the grant was approved under
    /// (e.g. where a capture will be sent). Shown to the user, written to
    /// the audit, and an action must state exactly the grant's terms. The
    /// kernel cannot check that they are true: they are claims on record.
    var terms: [String: String] = [:]
}

/// A refusal no approval can override (a focused password field, a
/// blocklisted app). Returns the reason, or nil to let the request through
/// to the grant check.
///
/// ``appliesAtGrant``: also checked when a grant is requested, so the user
/// is never even asked (a blocklisted app). Guards about the moment of
/// acting (what has focus, what is frontmost) apply to actions only.
protocol NeverApprovable {
    var appliesAtGrant: Bool { get }
    func refusal(for request: ActionRequest) -> String?
}

/// Who is asking, as authenticated at the channel (Caller.swift).
struct Requester {
    let description: String   // e.g. "Python (pid 123)"
    let audit: [String: Any]
}
