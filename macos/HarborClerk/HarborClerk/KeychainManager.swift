import Foundation
import Security
import os

struct KeychainCredentials {
    let email: String
    let password: String
}

/// Thin wrapper around the macOS Keychain for saving and loading login credentials.
///
/// No keychain access group. The one this used to declare (#445) is a
/// restricted entitlement that kills a Developer ID build at exec without an
/// embedded provisioning profile — and is ignored on the file-based login
/// keychain anyway. See MasterKeyManager in the server app for the measurements.
enum KeychainManager {
    private static let service = "com.harborclerk.HarborClerk"
    private static let logger = Logger(subsystem: "com.harborclerk.HarborClerk", category: "keychain")

    static func save(email: String, password: String) {
        // Delete any existing entry first
        delete()

        let passwordData = password.data(using: .utf8)!
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: email,
            kSecValueData as String: passwordData,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlocked,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        if status != errSecSuccess {
            // Logged but not surfaced — callers (AuthManager) don't currently
            // check a return value. errSecMissingEntitlement (-34018) here
            // means the signing chain isn't honoring the access-group
            // entitlement; the user will be re-prompted to log in on each
            // launch until the signing setup is fixed.
            logger.error("SecItemAdd failed: OSStatus \(status, privacy: .public)")
        }
    }

    static func load() -> KeychainCredentials? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecReturnAttributes as String: true,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess,
              let dict = item as? [String: Any],
              let email = dict[kSecAttrAccount as String] as? String,
              let data = dict[kSecValueData as String] as? Data,
              let password = String(data: data, encoding: .utf8)
        else {
            return nil
        }
        return KeychainCredentials(email: email, password: password)
    }

    static func delete() {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
        ]
        SecItemDelete(query as CFDictionary)
    }
}
