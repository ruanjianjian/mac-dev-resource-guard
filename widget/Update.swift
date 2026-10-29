import Foundation

/// After an automatic update the new app says so once (one-line-install AC-11): 「资源看板已更新到 1.7.8」 with the
/// release's first line. update.json is written by the background updater; the app only reads it and keeps its own
/// note of which version it already announced, so the two never write the same file.
enum UpdateNotice {
    static var folder: URL { stateHome().appendingPathComponent(".local/state/dev-resource-guard") }

    static func message(state: [String: Any], announced: String?, version: String) -> (title: String, body: String)? {
        guard let to = state["to"] as? String, to == version, announced != version,
              ["installing", "installed"].contains(state["status"] as? String ?? "") else { return nil }
        let summary = (state["summary"] as? String ?? "").trimmingCharacters(in: .whitespaces)
        return ("资源看板已更新到 \(version)", summary.isEmpty ? "新版已经装好，设置和权限都保留着。" : summary)
    }

    /// The notice for this launch, if this version came from an automatic update that has not been announced yet.
    static func pending(version: String?) -> (title: String, body: String)? {
        guard let version, let data = try? Data(contentsOf: folder.appendingPathComponent("update.json")),
              let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        let announced = try? String(contentsOf: folder.appendingPathComponent("update-announced"), encoding: .utf8)
        return message(state: state, announced: announced?.trimmingCharacters(in: .whitespacesAndNewlines), version: version)
    }

    static func markAnnounced(_ version: String) {
        try? version.write(to: folder.appendingPathComponent("update-announced"), atomically: true, encoding: .utf8)
    }

    static func selfTest() -> Bool {
        let state: [String: Any] = ["status": "installed", "to": "1.7.8", "summary": "菜单栏数字更好认"]
        guard let shown = message(state: state, announced: "1.7.7", version: "1.7.8"),
              shown.title == "资源看板已更新到 1.7.8", shown.body == "菜单栏数字更好认" else { return false }
        return message(state: state, announced: "1.7.8", version: "1.7.8") == nil            // said once
            && message(state: state, announced: nil, version: "1.7.7") == nil                 // not this version
            && message(state: ["status": "failed", "to": "1.7.8"], announced: nil, version: "1.7.8") == nil
            && message(state: ["status": "installing", "to": "1.7.8"], announced: nil, version: "1.7.8")?.body == "新版已经装好，设置和权限都保留着。"
    }
}
