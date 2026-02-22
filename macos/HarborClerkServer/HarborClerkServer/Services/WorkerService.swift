import Foundation

final class WorkerService: PythonService {
    private let queue: String

    init(queue: String, index: Int) {
        self.queue = queue
        super.init(name: "Worker-\(queue)-\(index)")
    }

    override var executableName: String { "harbor-clerk-worker" }
    override var arguments: [String] { ["--queues", queue] }
}
