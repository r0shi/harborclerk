import Foundation
import FoundationModels

// Parse args: --max-chars N, --mode summary|doc_type (default: summary)
var maxChars = 500
var mode = "summary"
let args = CommandLine.arguments
if let idx = args.firstIndex(of: "--max-chars"), idx + 1 < args.count,
   let val = Int(args[idx + 1])
{
    maxChars = val
}
if let idx = args.firstIndex(of: "--mode"), idx + 1 < args.count {
    mode = args[idx + 1]
}

// Read document text from stdin
guard let inputData = try? FileHandle.standardInput.readToEnd(),
      let inputText = String(data: inputData, encoding: .utf8),
      !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
else {
    fputs("Error: No input text on stdin\n", stderr)
    exit(1)
}

// Cap input to ~12,000 chars for the ~4K token context window
let cappedText = String(inputText.prefix(12_000))

// Build prompt based on mode
let prompt: String
switch mode {
case "doc_type":
    prompt = """
    Classify this document with a short phrase (2-4 words) describing what type of document it is. \
    Examples: Legal Contract, Tax Return, Meeting Notes, Research Paper, Invoice, Recipe, Resume, \
    Technical Manual, News Article, Personal Letter, Philosophical Essay, Novel, Religious Text, Diary. \
    Respond with ONLY the short phrase, no explanation.

    Document text:
    \(cappedText)
    """
default:
    prompt = """
    Summarize this document in 2-3 concise sentences. Be specific about the subject matter, \
    key points, and any conclusions. Stay under \(maxChars) characters.

    Document text:
    \(cappedText)
    """
}

// Call Foundation Models
Task {
    do {
        let session = LanguageModelSession()
        let response = try await session.respond(to: prompt)
        let summary = response.content.trimmingCharacters(in: .whitespacesAndNewlines)

        if summary.isEmpty {
            fputs("Error: Empty response from Foundation Models\n", stderr)
            exit(1)
        }

        print(summary)
        exit(0)
    } catch {
        fputs("Error: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
}

// Keep the run loop alive for the async task
RunLoop.main.run()
