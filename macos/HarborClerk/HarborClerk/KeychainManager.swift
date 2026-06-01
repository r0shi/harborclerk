import Foundation
import Security
import os

struct KeychainCredentials {
    let email: String
    let password: String
}

/// Thin wrapper around the macOS Keychain for saving and loading login credentials.
///
/// Items live in the shared keychain access group
/// `4HCL3BR49V.com.harborclerk.shared`, declared in HarborClerk.entitlements.
/// This anchors ACLs to the team identifier so rebuilds don't trigger the
/// per-binary "wants to access your keychain" prompt.
enum KeychainManager {
    private static let service = "com.harborclerk.HarborClerk"
    private static let accessGroup = "4HCL3BR49V.com.harborclerk.shared"
    private static let logger = Logger(subsystem: "com.harborclerk.HarborClerk", category: "keychain")

    static func save(email: String, password: String) {
        // Delete any existing entry first
        delete()

        let passwordData = password.data(using: .utf8)!
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: email,
            kSecAttrAccessGroup as String: accessGroup,
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
            kSecAttrAccessGroup as String: accessGroup,
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
            kSecAttrAccessGroup as String: accessGroup,
        ]
        SecItemDelete(query as CFDictionary)
    }
}
