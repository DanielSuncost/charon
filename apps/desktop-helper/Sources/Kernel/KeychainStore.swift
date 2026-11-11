// Where the audit key and head live: two generic-password items in the
// user's login keychain, created by this helper.
//
// macOS gives an item created this way an access list naming its creator
// (and a partition list naming its team), so this helper reads it silently
// while any other program, including Charon's Python, gets a password
// prompt from macOS that the user would have to approve. That prompt is
// the protection; it is a user decision, not a cryptographic wall.
//
// Considered and declined for now: a Secure Enclave key, which cannot be
// exported at all but needs a provisioning profile for this helper.

import Foundation
import Security

final class KeychainAuditStore: AuditKeyStore {
    let service = "ai.charon.desktop.audit"

    private func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    private func read(_ account: String) throws -> Data? {
        var q = query(account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        let s = SecItemCopyMatching(q as CFDictionary, &out)
        if s == errSecItemNotFound { return nil }
        guard s == errSecSuccess, let data = out as? Data else {
            throw AuditError.io("keychain read \(account): OSStatus \(s)")
        }
        return data
    }

    private func write(_ account: String, _ data: Data) throws {
        let s = SecItemUpdate(query(account) as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if s == errSecSuccess { return }
        guard s == errSecItemNotFound else { throw AuditError.io("keychain update \(account): OSStatus \(s)") }
        var add = query(account)
        add[kSecValueData as String] = data
        add[kSecAttrLabel as String] = "Charon Desktop audit \(account)"
        let a = SecItemAdd(add as CFDictionary, nil)
        guard a == errSecSuccess else { throw AuditError.io("keychain add \(account): OSStatus \(a)") }
    }

    func loadOrCreateKey() throws -> Data {
        if let k = try read("key"), k.count == 32 { return k }
        if try read("head") != nil {
            // A head without its key means the key was removed: refuse to
            // start a fresh chain silently over an existing one.
            throw AuditError.io("audit key is missing but a chain head exists")
        }
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            throw AuditError.io("no randomness for the audit key")
        }
        let key = Data(bytes)
        try write("key", key)
        return key
    }

    func readHead() throws -> (seq: Int, mac: String)? {
        guard let d = try read("head") else { return nil }
        guard let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let seq = obj["seq"] as? Int, let mac = obj["mac"] as? String else {
            // Never restart the chain over an unreadable head.
            throw AuditError.io("audit head in the keychain is unreadable")
        }
        return (seq, mac)
    }

    func writeHead(seq: Int, mac: String) throws {
        let d = try JSONSerialization.data(withJSONObject: ["seq": seq, "mac": mac], options: [.sortedKeys])
        try write("head", d)
    }
}
