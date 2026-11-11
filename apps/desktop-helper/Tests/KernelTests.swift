// Kernel tests, compiled with Sources/Kernel/*.swift by test.sh into a
// scratch binary. No keychain, no UI, no grants needed.

import CoreGraphics
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
    var last: Prompt?
    func ask(_ prompt: Prompt) -> PromptAnswer {
        asked += 1
        last = prompt
        return answers.isEmpty ? .deny("timeout") : answers.removeFirst()
    }
}

struct BlockApp: NeverApprovable {
    let blocked: String
    var appliesAtGrant: Bool { true }
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

final class Toggle: NeverApprovable {
    var on = false
    var appliesAtGrant: Bool { false }
    func refusal(for r: ActionRequest) -> String? { on ? "secure field focused" : nil }
}
let toggle = Toggle()
k.register(family: "desktop", classes: [], guards: [toggle])
prompter.answers = [.allow]
_ = try! k.requestGrant(classId: "desktop.input.text", target: safari, seconds: 300, reason: "", requester: me).get()
toggle.on = true
check(!allowed(k.authorize(typing(safari), requester: me)), "action-time guard overrides a live grant")
toggle.on = false
check(allowed(k.authorize(typing(safari), requester: me)), "and only while it applies")
_ = k.revokeAll(by: "test")

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

// ── Re-check after acting (stillAllowed)
let rp = ScriptedPrompter(), rs = MemoryStore()
let k5 = makeKernel(rs, rp, file: "recheck.jsonl")
let gate = Toggle()
k5.register(family: "desktop", classes: [], guards: [gate])
rp.answers = [.allow]
let g5 = try! k5.requestGrant(classId: "desktop.input.text", target: notes, seconds: 300, reason: "", requester: me).get()
let entries5 = k5.audit.verify().entries
check(k5.stillAllowed(typing(notes), grantId: g5.id) == nil, "recheck passes while guards and grant hold")
check(k5.audit.verify().entries == entries5, "recheck writes nothing to the audit")
gate.on = true
check(k5.stillAllowed(typing(notes), grantId: g5.id) != nil, "recheck fails when a guard starts refusing")
gate.on = false
check(k5.stillAllowed(typing(safari), grantId: g5.id) != nil, "recheck fails for a different target")
_ = k5.revoke(id: g5.id, by: "test")
check(k5.stillAllowed(typing(notes), grantId: g5.id) != nil, "recheck fails once the grant is revoked")
rp.answers = [.allow]
let g6 = try! k5.requestGrant(classId: "desktop.input.text", target: notes, seconds: 60, reason: "", requester: me).get()
clock = clock.addingTimeInterval(61)
check(k5.stillAllowed(typing(notes), grantId: g6.id) != nil, "recheck fails once the grant expires")

// ── Stated terms and caveats
let tp = ScriptedPrompter(), ts = MemoryStore()
let k6 = makeKernel(ts, tp, file: "terms.jsonl")
k6.register(family: "desktop", classes: [
    ActionClass(id: "desktop.observe.screen", family: "desktop", summary: "see", destructive: false, maxPerMinute: 30,
                caveat: "Only password fields are hidden."),
], guards: [])
let local = ["destination": "local qwen @ http://127.0.0.1:1234/v1", "destination_kind": "local-loopback"]
tp.answers = [.allow]
let g7 = try! k6.requestGrant(classId: "desktop.observe.screen", target: notes, seconds: 300, reason: "",
                              terms: local, requester: me).get()
let shown = tp.last?.lines.joined(separator: "\n") ?? ""
check(shown.contains("Only password fields are hidden."), "the class caveat is in the grant dialog")
check(shown.contains("not checked") && shown.contains("127.0.0.1:1234"), "stated terms are in the dialog, marked unchecked")
func look(_ t: [String: String]) -> ActionRequest { ActionRequest(classId: "desktop.observe.screen", target: notes, args: [:], terms: t) }
check(allowed(k6.authorize(look(local), requester: me)), "an action stating the grant's terms is allowed")
var cloud = local; cloud["destination"] = "anthropic @ https://api.anthropic.com"
check(!allowed(k6.authorize(look(cloud), requester: me)), "an action stating other terms is denied")
check(!allowed(k6.authorize(look([:]), requester: me)), "an action stating no terms is denied")
check(k6.stillAllowed(look(cloud), grantId: g7.id) != nil, "recheck fails when the terms differ")
let auditText = try! String(contentsOf: k6.audit.path, encoding: .utf8)
check(auditText.contains("127.0.0.1:1234") && auditText.contains("api.anthropic.com"),
      "the audit records stated destinations, allowed and denied")

// ── Capture core: coordinate mapping, redaction, retention
let mapped = pixelRects([CGRect(x: 110, y: 220, width: 50, height: 10)], origin: CGPoint(x: 100, y: 200),
                        scale: 2, imageSize: CGSize(width: 400, height: 400), pad: 0)
check(mapped == [CGRect(x: 20, y: 40, width: 100, height: 20)], "global points map to image pixels: \(mapped)")
check(pixelRects([CGRect(x: 0, y: 0, width: 10, height: 10)], origin: CGPoint(x: 100, y: 100), scale: 1,
                 imageSize: CGSize(width: 50, height: 50)).isEmpty, "rects outside the image are dropped")
let clipped = pixelRects([CGRect(x: 90, y: 90, width: 30, height: 30)], origin: CGPoint(x: 100, y: 100), scale: 1,
                         imageSize: CGSize(width: 50, height: 50), pad: 0)
check(clipped == [CGRect(x: 0, y: 0, width: 20, height: 20)], "partly visible rects are clipped, not dropped")

func whiteImage(_ w: Int, _ h: Int) -> CGImage {
    let c = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                      space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    c.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1)); c.fill(CGRect(x: 0, y: 0, width: w, height: h))
    return c.makeImage()!
}
func pixel(_ img: CGImage, _ x: Int, _ y: Int) -> UInt8 {   // red channel, top-left origin
    let c = CGContext(data: nil, width: img.width, height: img.height, bitsPerComponent: 8, bytesPerRow: img.width * 4,
                      space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    c.draw(img, in: CGRect(x: 0, y: 0, width: img.width, height: img.height))
    return c.data!.assumingMemoryBound(to: UInt8.self)[(y * img.width + x) * 4]
}
let red = redact(whiteImage(100, 80), [CGRect(x: 10, y: 5, width: 20, height: 10)])!
check(pixel(red, 15, 8) == 0, "redacted rect is black (top-left coordinates)")
check(pixel(red, 15, 70) == 255, "the mirrored position is untouched (no y-flip error)")
check(pixel(red, 50, 40) == 255, "outside the rect is untouched")
let png = encodePNG(red)!
check(png.starts(with: [0x89, 0x50, 0x4E, 0x47]), "encodes as PNG")

var capClock = Date(timeIntervalSince1970: 1_800_000_000)
let held = CaptureRetention(now: { capClock })
for i in 0..<(maxHeldCaptures + 3) {
    held.add(HeldCapture(id: "c\(i)", at: capClock, target: "t", png: png, sha256: "x"))
}
check(held.current().count == maxHeldCaptures, "retention holds at most \(maxHeldCaptures)")
check(held.current().first?.id == "c3", "the oldest are dropped first")
capClock = capClock.addingTimeInterval(heldCaptureSeconds)
check(held.current().isEmpty, "everything is gone after \(Int(heldCaptureSeconds))s")
held.add(HeldCapture(id: "n", at: capClock, target: "t", png: png, sha256: "x"))
check(held.clear() == 1 && held.current().isEmpty, "clear deletes what is held")

try? FileManager.default.removeItem(at: dir)
print(failures == 0 ? "ALL PASSED" : "\(failures) FAILED")
exit(failures == 0 ? 0 : 1)
