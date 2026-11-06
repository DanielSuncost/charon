// Caller authentication: who is on the other end of the channel.
//
// The channel is one end of an AF_UNIX socketpair that the parent creates
// and passes at spawn (``serve --fd N``). There is no filesystem path, port
// or name anyone else can connect to, so the only ways in are inheriting
// the fd or launching the helper yourself, and the second is what this
// check is for: the grants belong to this binary, so anyone who can execute
// it gets a process holding them.
//
// Identity comes from the kernel, not from a pid: LOCAL_PEERTOKEN gives the
// audit token the peer had when the socket was created, and the Security
// framework resolves that token (not a pid lookup, which races with reuse)
// to code and checks it against ``allowedCallerRequirement``. That
// requirement is generated at build time and compiled into this signed
// binary, so changing it means re-signing; no config, flag or environment
// variable can relax it. The peer must also be our parent process.
//
// What this does and does not prove, stated plainly: while the caller is a
// general interpreter (Homebrew Python), the strongest possible pin is that
// interpreter's exact cdhash, and every script it runs satisfies it. This
// check therefore refuses other binaries, other interpreters, a different
// build of Python and a swapped executable; it cannot tell Charon's Python
// from other Python code run by the same user. The guarantee that holds
// against that caller is that it can only *ask*: approvals are made by the
// user in the helper's own UI, which a process without the Accessibility
// grant cannot click for them.

import Foundation
import Security

struct CallerIdentity {
    let pid: pid_t
    let path: String
    let identifier: String
    let team: String
    let cdhash: String

    var json: [String: Any] {
        ["pid": Int(pid), "path": path, "identifier": identifier, "team": team, "cdhash": cdhash]
    }
}

enum CallerError: Error, CustomStringConvertible {
    case noToken(Int32)
    case notParent(pid_t, pid_t)
    case noCode(OSStatus)
    case badRequirement(OSStatus)
    case rejected(OSStatus, String)

    var description: String {
        switch self {
        case .noToken(let e): return "cannot read the caller's audit token (errno \(e))"
        case .notParent(let peer, let parent): return "caller pid \(peer) is not the parent \(parent)"
        case .noCode(let s): return "cannot resolve the caller's code (OSStatus \(s))"
        case .badRequirement(let s): return "built-in caller requirement is invalid (OSStatus \(s))"
        case .rejected(let s, let who): return "caller \(who) does not satisfy the built-in requirement (OSStatus \(s))"
        }
    }
}

func peerAuditToken(_ fd: Int32) throws -> audit_token_t {
    var token = audit_token_t()
    var len = socklen_t(MemoryLayout<audit_token_t>.size)
    if getsockopt(fd, SOL_LOCAL, LOCAL_PEERTOKEN, &token, &len) != 0 {
        throw CallerError.noToken(errno)
    }
    return token
}

func describe(_ code: SecCode) -> (String, String, String, String) {
    var staticCode: SecStaticCode?
    guard SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess, let sc = staticCode else { return ("", "", "", "") }
    var url: CFURL?
    SecCodeCopyPath(sc, [], &url)
    var info: CFDictionary?
    SecCodeCopySigningInformation(sc, SecCSFlags(rawValue: kSecCSSigningInformation), &info)
    let d = (info as? [String: Any]) ?? [:]
    let ident = d[kSecCodeInfoIdentifier as String] as? String ?? ""
    let team = d[kSecCodeInfoTeamIdentifier as String] as? String ?? ""
    let cdhash = (d[kSecCodeInfoUnique as String] as? Data)?.map { String(format: "%02x", $0) }.joined() ?? ""
    return ((url as URL?)?.path ?? "", ident, team, cdhash)
}

func authenticateCaller(fd: Int32, requirement: String) throws -> CallerIdentity {
    var token = try peerAuditToken(fd)
    // audit_token_t.val[5] is the pid; used for the parent check and for
    // display only. Identity is decided from the token itself below.
    let peerPid = pid_t(bitPattern: token.val.5)
    let parent = getppid()
    if peerPid != parent { throw CallerError.notParent(peerPid, parent) }

    let tokenData = Data(bytes: &token, count: MemoryLayout<audit_token_t>.size)
    var code: SecCode?
    let attrs = [kSecGuestAttributeAudit: tokenData] as CFDictionary
    let s = SecCodeCopyGuestWithAttributes(nil, attrs, [], &code)
    guard s == errSecSuccess, let peer = code else { throw CallerError.noCode(s) }

    var req: SecRequirement?
    let r = SecRequirementCreateWithString(requirement as CFString, [], &req)
    guard r == errSecSuccess, let requirementRef = req else { throw CallerError.badRequirement(r) }

    let (path, ident, team, cdhash) = describe(peer)
    let check = SecCodeCheckValidity(peer, [], requirementRef)
    if check != errSecSuccess {
        throw CallerError.rejected(check, "\(path) [\(ident) \(cdhash)]")
    }
    return CallerIdentity(pid: peerPid, path: path, identifier: ident, team: team, cdhash: cdhash)
}
