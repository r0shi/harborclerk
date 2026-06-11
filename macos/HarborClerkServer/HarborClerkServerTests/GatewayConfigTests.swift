import XCTest
@testable import HarborClerkServer

final class GatewayConfigTests: XCTestCase {
    func testDefaultPortMatchesReleaseGateway() {
        XCTAssertEqual(GatewayConfig.defaultPort, 8443)
    }

    func testLocalBaseURLUsesGatewayPort() {
        XCTAssertEqual(GatewayConfig.localBaseURL(hostname: "localhost", gatewayPort: 9443), "https://localhost:9443")
    }

    func testLocalBaseURLUsesHostname() {
        XCTAssertEqual(
            GatewayConfig.localBaseURL(hostname: "harbor.tailnet.ts.net", gatewayPort: 8443),
            "https://harbor.tailnet.ts.net:8443"
        )
    }

    func testLocalBaseURLNormalizesEnteredUrlsAndIpv6Hosts() {
        XCTAssertEqual(
            GatewayConfig.localBaseURL(hostname: "https://harbor.tailnet.ts.net:9443/t/key", gatewayPort: 8443),
            "https://harbor.tailnet.ts.net:8443"
        )
        XCTAssertEqual(
            GatewayConfig.localBaseURL(hostname: "https://[::1]:9443/t/key", gatewayPort: 8443),
            "https://[::1]:8443"
        )
    }

    func testCaddyfileProxiesLocalHttpsToApiPort() {
        let caddyfile = GatewayConfig.renderCaddyfile(apiPort: 8100, gatewayPort: 8443)

        XCTAssertTrue(caddyfile.contains("local_certs"))
        XCTAssertTrue(caddyfile.contains("skip_install_trust"))
        XCTAssertTrue(caddyfile.contains("auto_https disable_redirects"))
        XCTAssertTrue(caddyfile.contains("admin off"))
        XCTAssertTrue(caddyfile.contains("level INFO"))
        XCTAssertTrue(caddyfile.contains("https://localhost:8443"))
        XCTAssertTrue(caddyfile.contains("bind 127.0.0.1 ::1"))
        XCTAssertTrue(caddyfile.contains("tls internal"))
        XCTAssertTrue(caddyfile.contains("reverse_proxy 127.0.0.1:8100"))
    }

    func testExternalBindOnlyProxiesMcpPaths() {
        let caddyfile = GatewayConfig.renderCaddyfile(
            apiPort: 8100,
            gatewayPort: 8443,
            hostname: "harbor.tailnet.ts.net",
            bindAddresses: ["100.80.1.2"]
        )

        XCTAssertTrue(caddyfile.contains("https://harbor.tailnet.ts.net:8443"))
        XCTAssertTrue(caddyfile.contains("bind 100.80.1.2"))
        XCTAssertTrue(caddyfile.contains("handle /mcp*"))
        XCTAssertTrue(caddyfile.contains("handle /t*"))
        XCTAssertTrue(caddyfile.contains("respond 404"))
        XCTAssertFalse(caddyfile.contains("\n    reverse_proxy 127.0.0.1:8100\n"))
        XCTAssertFalse(caddyfile.contains("handle /api*"))
    }

    func testCustomCertificateIsRendered() {
        let caddyfile = GatewayConfig.renderCaddyfile(
            apiPort: 8100,
            gatewayPort: 8443,
            hostname: "harbor.example.com",
            certificateMode: .custom,
            certificatePath: "/Users/alex/certs/harbor cert.pem",
            privateKeyPath: "/Users/alex/certs/harbor key.pem"
        )

        XCTAssertTrue(caddyfile.contains(#"tls "/Users/alex/certs/harbor cert.pem" "/Users/alex/certs/harbor key.pem""#))
    }

    func testCaddyLogLevelMapsAppLogLevels() {
        XCTAssertEqual(GatewayConfig.caddyLogLevel("DEBUG"), "DEBUG")
        XCTAssertEqual(GatewayConfig.caddyLogLevel("WARNING"), "WARN")
        XCTAssertEqual(GatewayConfig.caddyLogLevel("WARN"), "WARN")
        XCTAssertEqual(GatewayConfig.caddyLogLevel("ERROR"), "ERROR")
        XCTAssertEqual(GatewayConfig.caddyLogLevel("TRACE"), "INFO")
        XCTAssertEqual(GatewayConfig.caddyLogLevel(""), "INFO")
    }
}
