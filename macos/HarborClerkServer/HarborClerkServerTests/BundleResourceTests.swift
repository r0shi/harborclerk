import XCTest
@testable import HarborClerkServer

/// Verify that the frontend build output is present in the app bundle.
/// If this test fails, run: cd frontend && npm run build
final class BundleResourceTests: XCTestCase {

    func testFrontendDistExists() {
        let frontendDist = Bundle.main.resourceURL!.appendingPathComponent("frontend-dist/index.html")
        XCTAssertTrue(FileManager.default.fileExists(atPath: frontendDist.path),
                      "frontend-dist/index.html missing — run 'cd frontend && npm run build'")
    }
}
