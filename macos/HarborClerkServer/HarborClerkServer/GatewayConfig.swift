import Foundation

enum GatewayCertificateMode: String {
    case `internal`
    case custom
}

struct GatewayConfig {
    static let defaultPort = 8443
    static let defaultHostname = "localhost"
    static let defaultBindAddresses = ["127.0.0.1", "::1"]

    let apiPort: Int
    let gatewayPort: Int
    let hostname: String
    let bindAddresses: [String]
    let certificateMode: GatewayCertificateMode
    let certificatePath: String
    let privateKeyPath: String

    var localBaseURL: String {
        Self.localBaseURL(hostname: hostname, gatewayPort: gatewayPort)
    }

    var caddyfile: String {
        Self.renderCaddyfile(
            apiPort: apiPort,
            gatewayPort: gatewayPort,
            hostname: hostname,
            bindAddresses: bindAddresses,
            certificateMode: certificateMode,
            certificatePath: certificatePath,
            privateKeyPath: privateKeyPath
        )
    }

    init(
        apiPort: Int,
        gatewayPort: Int,
        hostname: String = Self.defaultHostname,
        bindAddresses: [String] = Self.defaultBindAddresses,
        certificateMode: GatewayCertificateMode = .internal,
        certificatePath: String = "",
        privateKeyPath: String = ""
    ) {
        self.apiPort = apiPort
        self.gatewayPort = gatewayPort
        self.hostname = Self.normalizedHostname(hostname)
        self.bindAddresses = Self.normalizedBindAddresses(bindAddresses)
        self.certificateMode = certificateMode
        self.certificatePath = certificatePath.trimmingCharacters(in: .whitespacesAndNewlines)
        self.privateKeyPath = privateKeyPath.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func localBaseURL(hostname: String, gatewayPort: Int) -> String {
        "https://\(urlHost(normalizedHostname(hostname))):\(gatewayPort)"
    }

    static func normalizedHostname(_ hostname: String) -> String {
        var value = hostname.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.hasPrefix("https://") {
            value.removeFirst("https://".count)
        } else if value.hasPrefix("http://") {
            value.removeFirst("http://".count)
        }
        if let slash = value.firstIndex(of: "/") {
            value = String(value[..<slash])
        }
        if value.hasPrefix("["),
           let closeBracket = value.firstIndex(of: "]") {
            let hostStart = value.index(after: value.startIndex)
            value = String(value[hostStart..<closeBracket])
            return value.isEmpty ? defaultHostname : value
        }
        if let colon = value.lastIndex(of: ":"), value.firstIndex(of: ":") == colon {
            let suffix = value[value.index(after: colon)...]
            if Int(suffix) != nil {
                value = String(value[..<colon])
            }
        }
        value = value.trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        return value.isEmpty ? defaultHostname : value
    }

    static func normalizedBindAddresses(_ addresses: [String]) -> [String] {
        var seen = Set<String>()
        let normalized = addresses
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .filter { seen.insert($0).inserted }
        return normalized.isEmpty ? defaultBindAddresses : normalized
    }

    static func isLoopbackBind(_ address: String) -> Bool {
        address == "127.0.0.1" || address == "::1" || address == "localhost"
    }

    static func exposesFullApp(bindAddresses: [String]) -> Bool {
        normalizedBindAddresses(bindAddresses).allSatisfy(isLoopbackBind)
    }

    static func renderCaddyfile(
        apiPort: Int,
        gatewayPort: Int,
        hostname: String = defaultHostname,
        bindAddresses: [String] = defaultBindAddresses,
        certificateMode: GatewayCertificateMode = .internal,
        certificatePath: String = "",
        privateKeyPath: String = ""
    ) -> String {
        let cleanHostname = normalizedHostname(hostname)
        let binds = normalizedBindAddresses(bindAddresses)
        let site = "https://\(urlHost(cleanHostname)):\(gatewayPort)"
        let tlsLine: String
        if certificateMode == .custom,
           !certificatePath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
           !privateKeyPath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            tlsLine = "tls \(quote(certificatePath)) \(quote(privateKeyPath))"
        } else {
            tlsLine = "tls internal"
        }
        let proxyDirectives: String
        if exposesFullApp(bindAddresses: binds) {
            proxyDirectives = "    reverse_proxy 127.0.0.1:\(apiPort)"
        } else {
            proxyDirectives = """
                handle /mcp* {
                    reverse_proxy 127.0.0.1:\(apiPort)
                }
                handle /t* {
                    reverse_proxy 127.0.0.1:\(apiPort)
                }
                handle {
                    respond 404
                }
            """
        }

        return """
        {
            local_certs
            skip_install_trust
            admin off
        }

        \(site) {
            bind \(binds.joined(separator: " "))
            \(tlsLine)
        \(proxyDirectives)
        }
        """
    }

    static func caddyExecutable(resourceURL: URL = Bundle.main.resourceURL!) -> URL? {
        let bundled = resourceURL.appendingPathComponent("caddy/bin/caddy")
        if FileManager.default.isExecutableFile(atPath: bundled.path) {
            return bundled
        }

        for path in ["/opt/homebrew/bin/caddy", "/usr/local/bin/caddy"] {
            if FileManager.default.isExecutableFile(atPath: path) {
                return URL(fileURLWithPath: path)
            }
        }
        return nil
    }

    private static func urlHost(_ hostname: String) -> String {
        hostname.contains(":") ? "[\(hostname)]" : hostname
    }

    private static func quote(_ value: String) -> String {
        let escaped = value
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        return "\"\(escaped)\""
    }
}

private final class LocalCertificateBypassDelegate: NSObject, URLSessionDelegate {
    let allowedHosts: Set<String>

    init(allowedHosts: Set<String>) {
        self.allowedHosts = allowedHosts
    }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        let host = challenge.protectionSpace.host
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              allowedHosts.contains(host),
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }

        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

func httpsProbeOKAllowingLocalCertificate(
    _ url: URL,
    timeout: TimeInterval = 3,
    allowedHosts: Set<String> = ["localhost", "127.0.0.1", "::1"]
) async -> Bool {
    let config = URLSessionConfiguration.ephemeral
    config.timeoutIntervalForRequest = timeout
    config.timeoutIntervalForResource = timeout
    config.urlCache = nil
    config.httpCookieStorage = nil
    config.urlCredentialStorage = nil
    let delegate = LocalCertificateBypassDelegate(allowedHosts: allowedHosts)
    let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
    defer { session.invalidateAndCancel() }

    do {
        let (_, response) = try await session.data(from: url)
        guard let statusCode = (response as? HTTPURLResponse)?.statusCode else { return false }
        return statusCode >= 200 && statusCode < 500
    } catch {
        return false
    }
}
