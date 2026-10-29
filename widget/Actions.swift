import AppKit
import SwiftUI

struct ResourceRow: Decodable, Identifiable {
    let id: String
    let title: String
    let detail: String
    let category: String
    let bytes: Int64?
    let eligible: Bool
    let reason: String
    let outcome: String?
    let command: String?
    let kind: String?
    let loses: String?
    var tag: String? = nil    // memory page status: free / use / link / auto
}

struct BackendReply: Decodable {
    let status: String
    let message: String
    let rows: [ResourceRow]?
    let token: String?
    let created: Double?
    let eligible: Int?
    let eligibleBytes: Int64?
}

func testHomeArgument(_ arguments: [String]) throws -> String? {
    guard let index = arguments.firstIndex(of: "--test-home") else { return nil }
    guard index + 1 < arguments.count, !arguments[index + 1].hasPrefix("--"), !arguments[index + 1].isEmpty else {
        throw NSError(domain: "Dashboard", code: 2, userInfo: [NSLocalizedDescriptionKey: "隔离测试参数缺少目录，拒绝回退到真实工具。"])
    }
    return arguments[index + 1]
}

/// The Python backend behind the panel: every action is one short-lived process.
enum DashboardActions {
    /// One synchronous backend call; the panel runs it off the main thread.
    static func call(_ action: String, token: String? = nil, path: String? = nil, ids: [String]? = nil) throws -> BackendReply {
        guard let script = Bundle.main.url(forResource: "backend", withExtension: "py") else {
            throw NSError(domain: "Dashboard", code: 1, userInfo: [NSLocalizedDescriptionKey: "应用缺少后端适配器，请重新安装。"])
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        var args = [script.path, action]
        if let token { args += ["--token", token] }
        if let path { args += ["--path", path] }
        if let ids, !ids.isEmpty { args += ["--ids", ids.joined(separator: ",")] }
        // An explicit, marked disposable home is only for UI integration tests.
        if let home = try testHomeArgument(CommandLine.arguments) {
            guard FileManager.default.fileExists(atPath: home + "/.resource-dashboard-fixture") else {
                throw NSError(domain: "Dashboard", code: 2, userInfo: [NSLocalizedDescriptionKey: "隔离测试目录缺少标记，拒绝回退到真实工具。"])
            }
            args += ["--home", home]
            var env = ProcessInfo.processInfo.environment
            env["HOME"] = home
            task.environment = env
        }
        task.arguments = args
        let pipe = Pipe(); task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        try task.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        guard task.terminationStatus == 0 else {
            throw NSError(domain: "Dashboard", code: 3, userInfo: [NSLocalizedDescriptionKey: "后端连接失败，请检查工具安装。"])
        }
        return try JSONDecoder().decode(BackendReply.self, from: data)
    }
}
