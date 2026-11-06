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
// Channel: one end of an AF_UNIX socketpair the parent passes at spawn
// (``serve --fd N``); JSON lines both ways. The caller is authenticated by
// its audit token before any request is read (Caller.swift). EOF on the
// channel means the parent is gone; the helper exits. It has no network
// code and opens no files of its own choosing.
//
// Capability is deliberately narrow and grows only with the work.

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

/// The authenticated channel. Writes are serialised because the kill
/// switch reports from its own thread.
final class Channel {
    let fd: Int32
    private var buffer = Data()
    private let writeLock = NSLock()

    init(fd: Int32) { self.fd = fd }

    func send(_ obj: [String: Any]) {
        guard var data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]) else { return }
        data.append(0x0A)
        writeLock.lock(); defer { writeLock.unlock() }
        data.withUnsafeBytes { raw in
            var off = 0
            while off < raw.count {
                let n = write(fd, raw.baseAddress! + off, raw.count - off)
                if n < 0 && errno == EINTR { continue }
                if n <= 0 { return }
                off += n
            }
        }
    }

    /// Next line, or nil at EOF.
    func readLine() -> String? {
        var chunk = [UInt8](repeating: 0, count: 65536)
        while true {
            if let nl = buffer.firstIndex(of: 0x0A) {
                let line = buffer[buffer.startIndex..<nl]
                buffer = Data(buffer[buffer.index(after: nl)...])
                return String(decoding: line, as: UTF8.self)
            }
            let n = read(fd, &chunk, chunk.count)
            if n < 0 && errno == EINTR { continue }
            if n <= 0 { return nil }
            buffer.append(contentsOf: chunk[0..<n])
        }
    }
}

func handle(_ req: [String: Any], caller: CallerIdentity) -> [String: Any] {
    let id = req["id"] ?? NSNull()
    let cmd = req["cmd"] as? String ?? ""
    switch cmd {
    case "hello":
        return ["id": id, "ok": true, "identity": identity(), "caller": caller.json]
    case "permissions":
        return ["id": id, "ok": true, "grants": grants()]
    case "killswitch_status":
        return ["id": id, "ok": true, "killswitch": KillSwitch.shared.status()]
    case "killswitch_arm":
        let (ok, why) = KillSwitch.shared.arm()
        return ["id": id, "ok": ok, "note": why, "killswitch": KillSwitch.shared.status()]
    case "killswitch_selftest":
        // Bounded so the verb cannot become a long-running injector.
        let seconds = min(max((req["seconds"] as? Double) ?? 10, 1), 30)
        let r = killSwitchSelfTest(seconds: seconds)
        return ["id": id, "ok": true, "completed": r.completed, "total": r.total,
                "interrupted": r.interrupted ?? NSNull(), "restored": r.restored]
    case "request_access":
        let grant = req["grant"] as? String ?? ""
        guard let after = requestAccess(grant) else { return ["id": id, "ok": false, "error": "unknown grant: \(grant)"] }
        return ["id": id, "ok": true, "grant": grant, "granted": after]
    default:
        return ["id": id, "ok": false, "error": "unknown command: \(cmd)"]
    }
}

func serve(_ channel: Channel, caller: CallerIdentity) -> Never {
    while let line = channel.readLine() {
        if line.isEmpty { continue }
        guard let data = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            channel.send(["ok": false, "error": "malformed request"])
            continue
        }
        channel.send(handle(obj, caller: caller))
    }
    exit(0)  // channel closed: the parent is gone
}

func fail(_ fd: Int32?, _ message: String, code: Int32) -> Never {
    if let fd = fd { Channel(fd: fd).send(["ok": false, "fatal": true, "error": message]) }
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(code)
}

let args = CommandLine.arguments
guard args.count == 4, args[1] == "serve", args[2] == "--fd", let fd = Int32(args[3]), fd > 2,
      fcntl(fd, F_GETFD) != -1 else {
    fail(nil, "usage: CharonDesktop serve --fd N (an inherited socket)", code: 64)
}
var st = stat()
guard fstat(fd, &st) == 0, (st.st_mode & S_IFMT) == S_IFSOCK else {
    fail(nil, "fd \(fd) is not a socket", code: 64)
}
if ProcessInfo.processInfo.environment[disclaimedMarker] == nil {
    let why = reexecDisclaimed()
    fail(fd, "could not take responsibility for own grants: \(why)", code: 70)
}
unsetenv(disclaimedMarker)
let channel = Channel(fd: fd)
let caller: CallerIdentity
do {
    caller = try authenticateCaller(fd: fd, requirement: allowedCallerRequirement)
} catch {
    fail(fd, "caller refused: \(error)", code: 77)
}
KillSwitch.shared.onTrip = { why in channel.send(["event": "paused", "reason": why]) }
_ = KillSwitch.shared.start()  // false without Input Monitoring: stays tripped, nothing can run
// Requests are served on their own thread; the main thread runs the main
// loop so AppKit calls (re-activating the user's app) behave.
let server = Thread { serve(channel, caller: caller) }
server.name = "channel"
server.start()
RunLoop.main.run()
