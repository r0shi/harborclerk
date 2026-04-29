import Foundation

final class APIService: PythonService {
    init() {
        super.init(name: "API")
    }

    override var executableName: String { "harbor-clerk-api" }

    override func healthCheck() async -> Bool {
        // Process alive ⇒ healthy. The API event loop can be temporarily
        // blocked during research/synthesis (LLM prompt processing, DB
        // commits) and that's not a crash, so we deliberately don't gate
        // on an HTTP probe here. The previous implementation kept an
        // ephemeral URLSession probe alongside the process check, but
        // its return value was effectively ignored — alive-process
        // always reported healthy regardless of HTTP outcome — so the
        // probe was just a wasted in-flight URLSession task per
        // healthCheck tick. Dropped to reduce CFNetwork pressure during
        // model-load transitions (see
        // project_menubar_crashes_during_model_switch.md).
        return process?.isRunning == true
    }
}
