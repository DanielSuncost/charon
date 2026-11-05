// CharonDesktop (ai.charon.desktop): the one process that holds Charon's
// macOS desktop grants — Screen Recording, Accessibility, Input Monitoring.
//
// Why a separate signed helper: TCC attaches a grant to the code identity of
// the responsible process. Without this helper that is the Python
// interpreter, and a grant to it would hand screen capture and input
// injection to every script that interpreter runs. This binary is signed
// with a stable Developer ID, so the grant covers exactly this code and
// survives rebuilds.
//
// Responsibility: on start the helper re-execs itself in place (same pid,
// so the parent's pipes and wait() still work) with responsibility
// disclaimed, so macOS attributes its requests to this binary, not to
// Python, the terminal, or tmux, whichever launched it.
//
// Protocol: one JSON object per line on stdin, one JSON reply per line on
// stdout. EOF on stdin means the parent is gone; the helper exits. It has
// no network code and opens no files of its own choosing.
//
// Capability is deliberately narrow and grows only with the work: this
// build answers identity and grant questions and nothing else.

import ApplicationServices
import CoreGraphics
import Darwin
import Foundation

let disclaimedMarker = "CHARON_DESKTOP_DISCLAIMED"
let protocolVersion = 1

// ── Responsibility ──────────────────────────────────────────────────

typealias DisclaimFn = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32
typealias ResponsibleFn = @convention(c) (pid_t) -> pid_t

func sym<T>(_ name: String, _ type: T.Type) -> T? {
    guard let p = dlsym(UnsafeMutableRawPointer(bitPattern: -2), name) else { return nil }  // RTLD_DEFAULT
    return unsafeBitCast(p, to: type)
}

func responsiblePid(_ pid: pid_t) -> pid_t? {
    guard let fn = sym("responsibility_get_pid_responsible_for_pid", ResponsibleFn.self) else { return nil }
    let r = fn(pid)
    return r > 0 ? r : nil
}

func executablePath(_ pid: pid_t) -> String? {
    var buf = [CChar](repeating: 0, count: 4096)
    let n = proc_pidpath(pid, &buf, UInt32(buf.count))
    return n > 0 ? String(cString: buf) : nil
}

/// Replace this process image with itself, responsibility disclaimed.
/// Returns only on failure; the caller then refuses to serve.
func reexecDisclaimed() -> String {
    guard let disclaim = sym("responsibility_spawnattrs_setdisclaim", DisclaimFn.self) else {
        return "responsibility_spawnattrs_setdisclaim unavailable"
    }
    guard let exe = executablePath(getpid()) else { return "cannot resolve own executable" }
    var attr: posix_spawnattr_t?
    posix_spawnattr_init(&attr)
    defer { posix_spawnattr_destroy(&attr) }
    posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETEXEC))
    let rc = disclaim(&attr, 1)
    if rc != 0 { return "disclaim failed: \(rc)" }
    setenv(disclaimedMarker, "1", 1)
    var argv: [UnsafeMutablePointer<CChar>?] = CommandLine.arguments.map { strdup($0) } + [nil]
    var envp: [UnsafeMutablePointer<CChar>?] = ProcessInfo.processInfo.environment.map { strdup("\($0)=\($1)") } + [nil]
    var pid: pid_t = 0
    let err = posix_spawn(&pid, exe, nil, &attr, &argv, &envp)
    return "posix_spawn(SETEXEC) failed: \(String(cString: strerror(err)))"
}

// ── Grants ──────────────────────────────────────────────────────────

func grants() -> [String: Bool] {
    return [
        "screen_recording": CGPreflightScreenCaptureAccess(),
        "accessibility": AXIsProcessTrusted() && CGPreflightPostEventAccess(),
        "input_monitoring": CGPreflightListenEventAccess(),
    ]
}

func requestAccess(_ grant: String) -> Bool? {
    switch grant {
    case "screen_recording":
        _ = CGRequestScreenCaptureAccess()
    case "accessibility":
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
        _ = CGRequestPostEventAccess()
    case "input_monitoring":
        _ = CGRequestListenEventAccess()
    default:
        return nil
    }
    return grants()[grant]
}

func identity() -> [String: Any] {
    let pid = getpid()
    let rpid = responsiblePid(pid)
    return [
        "pid": Int(pid),
        "responsible_pid": rpid.map { Int($0) } ?? NSNull(),
        "is_self_responsible": rpid == pid,
        "executable": executablePath(pid) ?? "",
        "bundle": Bundle.main.bundlePath,
        "bundle_id": Bundle.main.bundleIdentifier ?? "",
        "version": Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "",
        "protocol": protocolVersion,
    ]
}

// ── Serve ───────────────────────────────────────────────────────────

func reply(_ obj: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]) else { return }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

func handle(_ req: [String: Any]) -> [String: Any] {
    let id = req["id"] ?? NSNull()
    let cmd = req["cmd"] as? String ?? ""
    switch cmd {
    case "hello":
        return ["id": id, "ok": true, "identity": identity()]
    case "permissions":
        return ["id": id, "ok": true, "grants": grants()]
    case "request_access":
        let grant = req["grant"] as? String ?? ""
        guard let after = requestAccess(grant) else { return ["id": id, "ok": false, "error": "unknown grant: \(grant)"] }
        return ["id": id, "ok": true, "grant": grant, "granted": after]
    default:
        return ["id": id, "ok": false, "error": "unknown command: \(cmd)"]
    }
}

func serve() -> Never {
    setvbuf(stdout, nil, _IOLBF, 0)
    while let line = readLine(strippingNewline: true) {
        if line.isEmpty { continue }
        guard let data = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            reply(["ok": false, "error": "malformed request"])
            continue
        }
        reply(handle(obj))
    }
    exit(0)  // stdin closed: the parent is gone
}

let args = CommandLine.arguments
guard args.count >= 2, args[1] == "serve" else {
    FileHandle.standardError.write("usage: CharonDesktop serve\n".data(using: .utf8)!)
    exit(64)
}
if ProcessInfo.processInfo.environment[disclaimedMarker] == nil {
    let why = reexecDisclaimed()
    reply(["ok": false, "fatal": true, "error": "could not take responsibility for own grants: \(why)"])
    exit(70)
}
unsetenv(disclaimedMarker)
serve()
