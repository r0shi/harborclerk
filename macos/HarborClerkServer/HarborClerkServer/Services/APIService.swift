import Foundation

final class APIService: PythonService {
    init() {
        super.init(name: "API")
    }

    override var executableName: String { "harbor-clerk-api" }

    override func healthCheck() async -> Bool {
        // Liveness, not just process-alive. The python process can stay alive
        // while the uvicorn listener stops accepting/serving connections — the
        // "zombie" failure mode observed after the Mac sleeps and wakes (see
        // project_menubar_process_management_audit.md / the PR #385 idle-
        // unreachable note). A process-only check never reports that as
        // unhealthy, so the HealthChecker's auto-restart never fires and the
        // whole app stays unreachable until the user manually restarts.
        //
        // We probe the dependency-free /api/system/ping (NOT /system/health,
        // which checks Postgres/Tika/reranker — a slow dependency there would
        // wrongly fail an API-liveness check and bounce a healthy API). A 200
        // means the event loop is turning. The stdout-pipe deadlock that used
        // to block the loop mid-research — the original reason this was
        // process-only — was fixed in PR #190, so a failing probe now
        // genuinely indicates a dead listener rather than a busy one.
        //
        // httpProbeOK uses an ephemeral, short-timeout, invalidated URLSession
        // (the sanctioned pattern that avoids the CFNetwork pointer-auth crash
        // from concurrent URLSession.shared probes during model load —
        // project_menubar_crashes_during_model_switch.md).
        //
        // The HealthChecker needs consecutiveFailuresBeforeError (6 = 60s) of
        // these before it errors+restarts, which absorbs transient
        // unavailability (model-switch churn, a brief synchronous spike)
        // without false-tripping.
        guard process?.isRunning == true else { return false }
        let port = AppSettings.shared.apiPort
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/system/ping") else { return false }
        return await httpProbeOK(url)
    }
}
