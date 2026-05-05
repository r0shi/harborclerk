import XCTest
@testable import HarborClerkServer

final class MasterKeyManagerTests: XCTestCase {

    /// Use a unique service id per test so concurrent runs don't trample each other.
    private func makeManager() -> MasterKeyManager {
        let id = "com.harborclerk.test.\(UUID().uuidString)"
        return MasterKeyManager(serviceIdentifier: id)
    }

    override func tearDown() {
        // Each makeManager() uses a unique id, so leaks between tests are bounded;
        // explicit cleanup happens inside each test.
        super.tearDown()
    }

    func test_load_returns_nil_when_no_key_stored() {
        let manager = makeManager()
        defer { manager.delete() }
        XCTAssertNil(manager.load())
    }

    func test_generate_and_load_returns_32_bytes() {
        let manager = makeManager()
        defer { manager.delete() }

        let generated = manager.generate()
        XCTAssertEqual(generated.count, 32)

        let loaded = manager.load()
        XCTAssertEqual(loaded, generated)
    }

    func test_generate_is_idempotent_via_loadOrGenerate() {
        let manager = makeManager()
        defer { manager.delete() }

        let first = manager.loadOrGenerate()
        let second = manager.loadOrGenerate()
        XCTAssertEqual(first, second, "loadOrGenerate must reuse the existing key")
    }

    func test_delete_removes_the_key() {
        let manager = makeManager()
        defer { manager.delete() }   // safety net even though the test calls delete itself
        _ = manager.generate()
        XCTAssertNotNil(manager.load())

        manager.delete()
        XCTAssertNil(manager.load())
    }

    func test_base64_encoded_is_44_chars() {
        let manager = makeManager()
        defer { manager.delete() }

        let key = manager.loadOrGenerate()
        let encoded = manager.base64Encoded(key: key)
        // 32 bytes → 44 chars base64 (with padding)
        XCTAssertEqual(encoded.count, 44)
    }
}
