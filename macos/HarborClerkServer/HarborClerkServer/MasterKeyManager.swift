import Foundation
import Security
import os

/// Owns the master encryption key in the user's login Keychain.
///
/// The master key is 32 random bytes generated via SecRandomCopyBytes on
/// first launch. Only the HarborClerkServer (menubar) app reads/writes it —
/// the four Python subprocesses receive it as the HARBOR_CLERK_MASTER_KEY
/// env var, which inherits naturally to children and never touches disk.
///
/// Stored under a unique Keychain service identifier so multiple installs
/// (development, release) don't trample each other. Production uses
/// `MasterKeyManager.production`; tests pass a unique id per run.
///
/// All items are written into the shared keychain access group
/// `4HCL3BR49V.com.harborclerk.shared`, declared in both apps' entitlements.
/// This anchors ACLs to the team identifier instead of the per-build
/// designated requirement, so rebuilds (dev cert ↔ release cert, ad-hoc ↔
/// signed) no longer trigger the "binary X wants to access your keychain"
/// prompt.
final class MasterKeyManager {
    static let production = MasterKeyManager(serviceIdentifier: "com.bitblot.harborclerk.master-key")

    private let serviceIdentifier: String
    private let account = "master-key"
    private let accessGroup: String?

    /// `accessGroup` defaults to the production shared group. Tests can pass
    /// `nil` to bypass it when running in a signing environment that doesn't
    /// honor the entitlement (e.g., ad-hoc CI).
    init(serviceIdentifier: String, accessGroup: String? = "4HCL3BR49V.com.harborclerk.shared") {
        self.serviceIdentifier = serviceIdentifier
        self.accessGroup = accessGroup
    }

    /// Read the stored key, or nil if no key is stored.
    func load() -> Data? {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: serviceIdentifier,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        if let accessGroup {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else {
            return nil
        }
        return data
    }

    /// Generate a new 32-byte key and persist it.
    ///
    /// **DESTRUCTIVE: overwrites any existing key.** Calling this when a key is
    /// already stored renders all previously-encrypted ciphertext unreadable
    /// (KeyMismatch on every decrypt). Normal app startup must use
    /// `loadOrGenerate()` instead. This method is exposed only for an explicit
    /// operator-initiated reset flow.
    @discardableResult
    func generate() -> Data {
        var bytes = [UInt8](repeating: 0, count: 32)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        precondition(status == errSecSuccess, "SecRandomCopyBytes failed: \(status)")
        let data = Data(bytes)
        store(data)
        return data
    }

    /// Read the stored key, generating a new one on first use.
    /// This is the normal entry point for app startup.
    func loadOrGenerate() -> Data {
        if let existing = load() {
            return existing
        }
        return generate()
    }

    /// Remove the stored key. Use only for testing or operator-initiated reset.
    func delete() {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: serviceIdentifier,
            kSecAttrAccount as String: account,
        ]
        if let accessGroup {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        SecItemDelete(query as CFDictionary)
    }

    /// Convenience: return the key as the standard base64 the env var expects.
    func base64Encoded(key: Data) -> String {
        return key.base64EncodedString()
    }

    // MARK: - Internals

    private func store(_ data: Data) {
        // Delete-then-add is the standard idiom for "set a Keychain item".
        delete()
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: serviceIdentifier,
            kSecAttrAccount as String: account,
            kSecValueData as String: data,
            // ThisDeviceOnly: prevents iCloud Keychain sync of the master key. The spec's
            // recovery story is "lost master key = re-enter mail account passwords"
            // (docs/superpowers/specs/2026-05-04-email-ingestion-design.md), so binding
            // the key to this device is the right security tradeoff vs. WhenUnlocked.
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        ]
        if let accessGroup {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        let status = SecItemAdd(query as CFDictionary, nil)
        if status != errSecSuccess {
            // Don't crash. A failed persist (e.g., errSecMissingEntitlement when the
            // signing chain doesn't honor the keychain-access-groups entitlement, or
            // errSecInteractionNotAllowed if the Keychain is locked) used to fire a
            // precondition and SIGABRT on launch. Now we log loudly and let the
            // caller proceed with the in-memory key. On next launch load() will
            // return nil and a fresh key will be generated — previously-encrypted
            // secrets become unreadable, which matches the spec's recovery story
            // (re-enter mail-account passwords).
            Log.logger("master-key").error(
                "SecItemAdd failed for service \(self.serviceIdentifier, privacy: .public): OSStatus \(status, privacy: .public). Key is in memory but not persisted; data encrypted with it will not survive restart."
            )
        }
    }
}
