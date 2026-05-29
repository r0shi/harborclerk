import Foundation
import FoundationModels

// Daemon mode: read JSON request lines from stdin, write JSON response lines
// to stdout. The Python caller in src/harbor_clerk/llm/summarize.py caches a
// single Popen handle so the ~500ms `LanguageModelSession` construction cost
// is paid once at process startup instead of on every summarize call.
//
// Wire protocol (one JSON object per line, no trailing whitespace):
//   request:          {"prompt": "...", "max_chars": 500, "mode": "summary"|"doc_type"}
//   success response: {"result": "..."}
//   error response:   {"error": "..."}
//
// On EOF (stdin closed) the process exits 0 — the Python side respawns on
// next call if it needs another session. No graceful "drain" semantics:
// requests are processed strictly sequentially, so EOF after the last
// readLine() means the last in-flight response has already been written.

let session = LanguageModelSession()
let runLoopBlocker = DispatchSemaphore(value: 0)

Task {
    while let line = readLine() {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { continue }

        guard let data = trimmed.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let prompt = json["prompt"] as? String, !prompt.isEmpty
        else {
            writeJSON(["error": "Invalid request: expected {prompt, max_chars, mode}"])
            continue
        }

        let maxChars = (json["max_chars"] as? Int) ?? 500
        let mode = (json["mode"] as? String) ?? "summary"
        // Cap input to ~12,000 chars for the ~4K token context window
        let cappedText = String(prompt.prefix(12_000))
        let fullPrompt = buildPrompt(mode: mode, maxChars: maxChars, text: cappedText)

        do {
            let response = try await session.respond(to: fullPrompt)
            let result = response.content.trimmingCharacters(in: .whitespacesAndNewlines)
            if result.isEmpty {
                writeJSON(["error": "Empty response from Foundation Models"])
            } else {
                writeJSON(["result": result])
            }
        } catch {
            writeJSON(["error": error.localizedDescription])
        }
    }
    runLoopBlocker.signal()
}

runLoopBlocker.wait()
exit(0)

// MARK: - Helpers

func buildPrompt(mode: String, maxChars: Int, text: String) -> String {
    if mode == "doc_type" {
        return """
        Classify this document with a short phrase (2-4 words) describing what type of document it is. \
        Examples: Legal Contract, Tax Return, Meeting Notes, Research Paper, Invoice, Recipe, Resume, \
        Technical Manual, News Article, Personal Letter, Philosophical Essay, Novel, Religious Text, Diary. \
        Respond with ONLY the short phrase, no explanation.

        Document text:
        \(text)
        """
    }
    return """
    Summarize this document in 2-3 concise sentences. Be specific about the subject matter, \
    key points, and any conclusions. Stay under \(maxChars) characters.

    Document text:
    \(text)
    """
}

/// Write one JSON object per line to stdout. FileHandle is used directly
/// (instead of `print`) because Swift's stdout is block-buffered when the
/// caller is a pipe (not a terminal), and `print` doesn't flush between
/// writes — the Python reader would block forever waiting for output.
func writeJSON(_ dict: [String: String]) {
    guard let data = try? JSONSerialization.data(withJSONObject: dict, options: []) else { return }
    var line = data
    line.append(0x0A)  // newline
    FileHandle.standardOutput.write(line)
}
