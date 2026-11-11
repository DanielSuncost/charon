// A keyed, append-only audit chain the agent cannot forge.
//
// Why keyed: the agent runs as the same user and can rewrite any file it
// can read. A bare hash chain only proves internal consistency, and anyone
// who can write the file can recompute every hash, so it would be theatre.
// Here each entry carries an HMAC-SHA256 under a key that only this helper
// can read (see KeychainStore.swift), and the MAC covers the previous
// entry's MAC, so an entry cannot be edited, removed, reordered or invented
// without the key.
//
// Truncation: the key's keychain item also holds the latest (seq, mac).
// Cutting entries off the end, or deleting the file, leaves the file behind
// that head, which ``verify`` reports. New entries always chain from the
// keychain head, never from whatever the file's last line says, so a
// tampered file stays visibly broken rather than being papered over.
//
// Format: one entry per line, ``<mac hex> <body json>``. The MAC is over the
// body bytes exactly as written, so verification never depends on JSON
// re-serialisation. Bodies carry seq, ts, kind, prev (the previous MAC) and
// the entry's fields. Anyone may read the file; only the helper can extend
// it validly.
//
// Order of writes: file first, then head. A crash between them leaves one
// valid entry past the head, which verify reports as an unacknowledged
// tail rather than tampering (only the key holder could have written it).
//
// What it does not stop: deleting the keychain items (which macOS guards
// with a user prompt for any program but this one) or the user's own
// changes. It detects; it does not prevent.

import CryptoKit
import Foundation

protocol AuditKeyStore {
    func loadOrCreateKey() throws -> Data
    func readHead() throws -> (seq: Int, mac: String)?
    func writeHead(seq: Int, mac: String) throws
}

enum AuditError: Error, CustomStringConvertible {
    case io(String)
    case encoding
    var description: String {
        switch self {
        case .io(let s): return "audit write failed: \(s)"
        case .encoding: return "audit entry could not be encoded"
        }
    }
}

struct AuditVerification {
    let ok: Bool
    let entries: Int
    let headSeq: Int
    let problem: String?
    let note: String?

    var json: [String: Any] {
        ["ok": ok, "entries": entries, "head_seq": headSeq, "problem": problem ?? NSNull(), "note": note ?? NSNull()]
    }
}

final class AuditLog {
    let path: URL
    private let store: AuditKeyStore
    private let key: SymmetricKey
    private let lock = NSLock()
    private var headSeq: Int
    private var headMac: String
    static let genesis = "genesis"

    init(path: URL, store: AuditKeyStore) throws {
        self.path = path
        self.store = store
        self.key = SymmetricKey(data: try store.loadOrCreateKey())
        if let head = try store.readHead() {
            headSeq = head.seq
            headMac = head.mac
        } else {
            headSeq = 0
            headMac = AuditLog.genesis
        }
        let dir = path.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        if !FileManager.default.fileExists(atPath: path.path) {
            guard FileManager.default.createFile(atPath: path.path, contents: nil,
                                                 attributes: [.posixPermissions: 0o600]) else {
                throw AuditError.io("cannot create \(path.path)")
            }
        }
        try adoptUnacknowledgedTail()
    }

    /// After a crash between the file write and the head write, the file
    /// holds one valid entry chaining from the head. Adopt it so the next
    /// entry does not reuse its seq. Anything else is left for verify.
    private func adoptUnacknowledgedTail() throws {
        guard let data = FileManager.default.contents(atPath: path.path),
              let raw = data.split(separator: 0x0A, omittingEmptySubsequences: true).last else { return }
        let line = Data(raw)
        guard let space = line.firstIndex(of: 0x20) else { return }
        let storedMac = String(decoding: line[line.startIndex..<space], as: UTF8.self)
        let body = Data(line[line.index(after: space)...])
        guard mac(body) == storedMac,
              let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
              let seq = obj["seq"] as? Int, let prev = obj["prev"] as? String,
              seq == headSeq + 1, prev == headMac else { return }
        try store.writeHead(seq: seq, mac: storedMac)
        headSeq = seq
        headMac = storedMac
    }

    private func mac(_ body: Data) -> String {
        HMAC<SHA256>.authenticationCode(for: body, using: key).map { String(format: "%02x", $0) }.joined()
    }

    /// Append one entry and return its seq. Callers treat a throw as
    /// "do not act": nothing happens that the audit did not record.
    @discardableResult
    func append(_ kind: String, _ fields: [String: Any]) throws -> Int {
        lock.lock(); defer { lock.unlock() }
        let seq = headSeq + 1
        var body = fields
        body["seq"] = seq
        body["kind"] = kind
        body["prev"] = headMac
        body["ts"] = ISO8601DateFormatter().string(from: Date())
        guard JSONSerialization.isValidJSONObject(body),
              let bytes = try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys, .withoutEscapingSlashes])
        else { throw AuditError.encoding }
        let m = mac(bytes)
        var line = Data((m + " ").utf8)
        line.append(bytes)
        line.append(0x0A)
        guard let fh = FileHandle(forWritingAtPath: path.path) else { throw AuditError.io("cannot open \(path.path)") }
        defer { try? fh.close() }
        do {
            try fh.seekToEnd()
            try fh.write(contentsOf: line)
            try fh.synchronize()
        } catch {
            throw AuditError.io("\(error)")
        }
        try store.writeHead(seq: seq, mac: m)
        headSeq = seq
        headMac = m
        return seq
    }

    func verify() -> AuditVerification {
        lock.lock(); let hSeq = headSeq, hMac = headMac; lock.unlock()
        guard let data = FileManager.default.contents(atPath: path.path) else {
            return AuditVerification(ok: hSeq == 0, entries: 0, headSeq: hSeq,
                                     problem: hSeq == 0 ? nil : "audit file is missing; head says \(hSeq) entries", note: nil)
        }
        var prev = AuditLog.genesis
        var lastSeq = 0
        var count = 0
        for raw in data.split(separator: 0x0A, omittingEmptySubsequences: true) {
            let line = Data(raw)
            guard let space = line.firstIndex(of: 0x20) else {
                return broken(count, hSeq, "entry \(lastSeq + 1) is malformed")
            }
            let storedMac = String(decoding: line[line.startIndex..<space], as: UTF8.self)
            let body = Data(line[line.index(after: space)...])
            guard mac(body) == storedMac else {
                return broken(count, hSeq, "entry \(lastSeq + 1) fails its MAC (edited or forged)")
            }
            guard let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
                  let seq = obj["seq"] as? Int, let p = obj["prev"] as? String else {
                return broken(count, hSeq, "entry \(lastSeq + 1) has no seq/prev")
            }
            guard seq == lastSeq + 1, p == prev else {
                return broken(count, hSeq, "chain breaks at entry \(seq): expected seq \(lastSeq + 1) after \(prev.prefix(12))")
            }
            prev = storedMac
            lastSeq = seq
            count += 1
        }
        if lastSeq == hSeq && prev == hMac {
            return AuditVerification(ok: true, entries: count, headSeq: hSeq, problem: nil, note: nil)
        }
        if lastSeq < hSeq {
            return broken(count, hSeq, "file ends at entry \(lastSeq) but the keychain head is \(hSeq): truncated")
        }
        if lastSeq == hSeq + 1 {
            return AuditVerification(ok: true, entries: count, headSeq: hSeq, problem: nil,
                                     note: "one valid entry past the head (crash between file and head write)")
        }
        return broken(count, hSeq, "file runs to entry \(lastSeq) past the keychain head \(hSeq)")
    }

    private func broken(_ count: Int, _ head: Int, _ why: String) -> AuditVerification {
        AuditVerification(ok: false, entries: count, headSeq: head, problem: why, note: nil)
    }
}
