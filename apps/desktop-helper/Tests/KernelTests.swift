// Kernel tests, compiled with Sources/Kernel/*.swift by test.sh into a
// scratch binary. No keychain, no UI, no grants needed.

import CryptoKit
import Foundation

var failures = 0
func check(_ cond: Bool, _ what: String, line: Int = #line) {
    if cond { print("ok   \(what)") } else { failures += 1; print("FAIL \(what) (line \(line))") }
}

final class MemoryStore: AuditKeyStore {
    var key = Data((0..<32).map { UInt8($0) })
    var head: (seq: Int, mac: String)?
    var failWrites = false
    func loadOrCreateKey() throws -> Data { key }
    func readHead() throws -> (seq: Int, mac: String)? { head }
    func writeHead(seq: Int, mac: String) throws {
        if failWrites { throw AuditError.io("injected") }
        head = (seq, mac)
    }
}

final class ScriptedPrompter: Prompter {
    var answers: [PromptAnswer] = []
    var asked = 0
    func ask(_ prompt: Prompt) -> PromptAnswer {
        asked += 1
        return answers.isEmpty ? .deny("timeout") : answers.removeFirst()
    }
}

struct BlockApp: NeverApprovable {
    let blocked: String
    func refusal(for r: ActionRequest) -> String? { r.target.id == blocked ? "\(blocked) is blocklisted" : nil }
}

let dir = FileManager.default.temporaryDirectory.appendingPathComponent("kernel-test-\(getpid())")
try? FileManager.default.removeItem(at: dir)
var clock = Date(timeIntervalSince1970: 1_800_000_000)
func makeKernel(_ store: MemoryStore, _ prompter: ScriptedPrompter, file: String = "audit.jsonl") -> Kernel {
    let audit = try! AuditLog(path: dir.appendingPathComponent(file), store: store)
    let k = Kernel(audit: audit, prompter: prompter, now: { clock })
    k.register(family: "desktop", classes: [
        ActionClass(id: "desktop.input.text", family: "desktop", summary: "type text", destructive: false, maxPerMinute: 3),
        ActionClass(id: "desktop.input.destructive", family: "desktop", summary: "quit an app", destructive: true, maxPerMinute: 10),
        ActionClass(id: "other.thing", family: "other", summary: "not this family", destructive: false, maxPerMinute: 10),
    ], guards: [BlockApp(blocked: "com.apple.keychainaccess")])
    return k
}
let me = Requester(description: "test", audit: ["pid": 1])
let notes = Target(id: "com.apple.Notes", label: "Notes")
let safari = Target(id: "com.apple.Safari", label: "Safari")
func allowed(_ d: Decision) -> Bool { if case .allow = d { return true }; return false }
func typing(_ t: Target) -> ActionRequest { ActionRequest(classId: "desktop.input.text", target: t, args: ["chars": 3]) }

// ── Grants and decisions
let store = MemoryStore(), prompter = ScriptedPrompter()
let k = makeKernel(store, prompter)
check(!allowed(k.authorize(typing(notes), requester: me)), "default deny with no grant")
check(k.actionClass("other.thing") == nil, "a family cannot register another family's class")
check((try? k.requestGrant(classId: "desktop.input.text", target: notes, seconds: 300, reason: "", requester: me).get()) == nil,
      "unanswered prompt is a denial")
prompter.answers = [.allow]
let g = try! k.requestGrant(classId: "desktop.input.text", target: notes, seconds: 300, reason: "x", requester: me).get()
check(allowed(k.authorize(typing(notes), requester: me)), "grant allows its exact class and target")
check(!allowed(k.authorize(typing(safari), requester: me)), "grant does not cover another target")
check(!allowed(k.authorize(ActionRequest(classId: "desktop.input.destructive", target: notes, args: [:]), requester: me)),
      "grant does not cover another class")
check(!allowed(k.authorize(ActionRequest(classId: "nope", target: notes, args: [:]), requester: me)), "unknown class denied")
check((try? k.narrow(id: g.id, seconds: 7200, requester: me).get()) == nil, "narrow cannot extend")
check((try? k.narrow(id: g.id, seconds: 120, requester: me).get()) != nil, "narrow can shorten")
clock = clock.addingTimeInterval(121)
check(!allowed(k.authorize(typing(notes), requester: me)), "grant dies at expiry")
check(k.liveGrants().isEmpty, "expired grant is gone")

prompter.answers = [.allow]
let g2 = try! k.requestGrant(classId: "desktop.input.text", target: notes, seconds: 99999, reason: "", requester: me).get()
check(g2.expiresAt.timeIntervalSince(g2.grantedAt) == Kernel.maxGrantSeconds, "grant clamped to the maximum")
check(allowed(k.authorize(typing(notes), requester: me)), "1st action in a fresh minute")
check(allowed(k.authorize(typing(notes), requester: me)), "2nd action")
check(allowed(k.authorize(typing(notes), requester: me)), "3rd action (limit 3/min)")
check(!allowed(k.authorize(typing(notes), requester: me)), "4th action in the same minute is rate limited")
clock = clock.addingTimeInterval(61)
check(allowed(k.authorize(typing(notes), requester: me)), "rate window rolls")
check(k.revoke(id: g2.id, by: "test"), "revoke")
check(!allowed(k.authorize(typing(notes), requester: me)), "revoked grant denies")

prompter.answers = [.allow]
_ = try! k.requestGrant(classId: "desktop.input.destructive", target: notes, seconds: 600, reason: "", requester: me).get()
let destructive = ActionRequest(classId: "desktop.input.destructive", target: notes, args: ["key": "cmd+q"])
let before = prompter.asked
prompter.answers = [.allow]
check(allowed(k.authorize(destructive, requester: me)), "destructive allowed with per-action approval")
check(!allowed(k.authorize(destructive, requester: me)), "destructive denied when the next prompt goes unanswered")
check(prompter.asked == before + 2, "destructive prompts every time inside a live grant")

let keychain = Target(id: "com.apple.keychainaccess", label: "Keychain Access")
prompter.answers = [.allow]
let asked0 = prompter.asked
check((try? k.requestGrant(classId: "desktop.input.text", target: keychain, seconds: 60, reason: "", requester: me).get()) == nil,
      "never-approvable target cannot even be granted")
check(prompter.asked == asked0, "user is not asked about a never-approvable target")
prompter.answers = []
check(k.revokeAll(by: "test") == 1, "revoke all")

// ── Audit
let v = k.audit.verify()
check(v.ok && v.entries > 10, "audit verifies (\(v.entries) entries)")
let path = k.audit.path
let original = try! String(contentsOf: path, encoding: .utf8)
var lines = original.split(separator: "\n").map(String.init)

try! (lines.enumerated().map { $0.offset == 3 ? $0.element.replacingOccurrences(of: "com.apple.Notes", with: "com.apple.Mail") : $0.element }
    .joined(separator: "\n") + "\n").write(to: path, atomically: true, encoding: .utf8)
check(!k.audit.verify().ok, "edited entry detected: \(k.audit.verify().problem ?? "")")

var removed = lines; removed.remove(at: 4)
try! (removed.joined(separator: "\n") + "\n").write(to: path, atomically: true, encoding: .utf8)
check(!k.audit.verify().ok, "removed entry detected: \(k.audit.verify().problem ?? "")")

try! (lines.dropLast(2).joined(separator: "\n") + "\n").write(to: path, atomically: true, encoding: .utf8)
check(!k.audit.verify().ok, "truncation detected: \(k.audit.verify().problem ?? "")")

try! "".write(to: path, atomically: true, encoding: .utf8)
check(!k.audit.verify().ok, "emptied file detected")

// Forging without the key: rewrite an entry and recompute a plain SHA-256
// "mac" the way a bare hash chain would. It must not verify.
var forged = lines
let body = String(forged[2].split(separator: " ", maxSplits: 1)[1]).replacingOccurrences(of: "\"x\"", with: "\"y\"")
let fakeMac = SHA256.hash(data: Data(body.utf8)).map { String(format: "%02x", $0) }.joined()
forged[2] = fakeMac + " " + body
try! (forged.joined(separator: "\n") + "\n").write(to: path, atomically: true, encoding: .utf8)
check(!k.audit.verify().ok, "keyless forgery detected")

try! original.write(to: path, atomically: true, encoding: .utf8)
check(k.audit.verify().ok, "restored file verifies again")

// Crash between file write and head write: one valid entry past the head.
let crashStore = MemoryStore()
let crashPrompter = ScriptedPrompter()
let k2 = makeKernel(crashStore, crashPrompter, file: "crash.jsonl")
k2.log("one", [:])
let savedHead = crashStore.head
k2.log("two", [:])
crashStore.head = savedHead   // as if the helper died before writing the head
let k3 = makeKernel(crashStore, crashPrompter, file: "crash.jsonl")
k3.log("three", [:])
check(k3.audit.verify().ok && k3.audit.verify().note == nil, "restart adopts the tail and keeps chaining")

// Audit failure means no action.
let failing = MemoryStore()
let fp = ScriptedPrompter()
let k4 = makeKernel(failing, fp, file: "fail.jsonl")
fp.answers = [.allow]
_ = try! k4.requestGrant(classId: "desktop.input.text", target: notes, seconds: 300, reason: "", requester: me).get()
failing.failWrites = true
check(!allowed(k4.authorize(typing(notes), requester: me)), "audit write failure denies the action")

try? FileManager.default.removeItem(at: dir)
print(failures == 0 ? "ALL PASSED" : "\(failures) FAILED")
exit(failures == 0 ? 0 : 1)
