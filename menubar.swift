import Cocoa
import WebKit
import Darwin

// MARK: - 数据模型（与 Python 后台写入的 state.json 对应）

struct Relationship: Codable {
    var type: String
    var label: String?
    var confidence: Int?
    var reason: String?
}

struct TrendPrediction: Codable {
    var messages: Int?
    var score: Double?
    var low: Double?
    var high: Double?
}

struct TrendState: Codable {
    var status: String?
    var label: String?
    var symbol: String?
    var slope_per_100_msgs: Double?
    var span_messages: Int?
    var prediction: TrendPrediction?
    var description: String?
    var days: Int?
}

struct RecentTrend: Codable {
    var close: TrendState?
    var romantic: TrendState?
    var windows: Int?
    var description: String?
}

struct Chat: Codable {
    let id: String
    var name: String
    var score: Int
    var delta: Int
    var avatar: String?
    var reason: String?
    var unread: Int?
    var relationship: Relationship?
    var trend: TrendState?
    var recent_trend: RecentTrend?
    var dimensions: [String: Int]?
}

struct APIIssue: Codable {
    var code: String
    var message: String
}

struct RealtimeState: Codable {
    var connected: Bool?
    var message: String?
    var active: Int?
    var enabled: Bool?
    var profile: String?
}

struct AppState: Codable {
    var chats: [Chat]
    var updated_at: Double?
    var api_issue: APIIssue?
    var realtime: RealtimeState?
}

// MARK: - 应用

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {

    var statusItem: NSStatusItem!
    var stateURL: URL!
    var lastMtime: Date?
    var chats: [Chat] = []
    var apiIssue: APIIssue?
    var realtime: RealtimeState?
    var pinnedID: String?
    var refreshTimer: Timer?
    var dashboardWindow: NSWindow?
    var webView: WKWebView?
    var backendProcess: Process?
    var backendLog: FileHandle?
    var menuLock: Int32 = -1
    var dashboardRequest = 0
    var packagedApp: Bool { Bundle.main.bundleIdentifier == "local.zhijian.app" }
    var projectURL: URL {
        let path = Bundle.main.object(forInfoDictionaryKey: "WVProjectRoot") as? String
            ?? FileManager.default.currentDirectoryPath
        return URL(fileURLWithPath: path, isDirectory: true)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(packagedApp ? .regular : .accessory)
        let path = CommandLine.arguments.count > 1 && !packagedApp
            ? CommandLine.arguments[1]
            : projectURL.appendingPathComponent("state.json").path
        stateURL = URL(fileURLWithPath: path)
        let lockPath = stateURL.deletingLastPathComponent().appendingPathComponent(".wvbar.lock").path
        menuLock = Darwin.open(lockPath, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard menuLock >= 0, flock(menuLock, LOCK_EX | LOCK_NB) == 0 else {
            if packagedApp {
                NSRunningApplication.runningApplications(withBundleIdentifier: "local.zhijian.app")
                    .first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier })?
                    .activate(options: .activateAllWindows)
            }
            NSApp.terminate(nil)
            return
        }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "❤ –"
        statusItem.button?.toolTip = "知间 · 聊天关系"

        pinnedID = UserDefaults.standard.string(forKey: "wvbar.pinnedID")

        loadState()
        rebuildMenu()

        refreshTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
        if packagedApp { installAppMenu(); showDashboard() }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showDashboard()
        return true
    }

    func windowWillClose(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let child = backendProcess, child.isRunning { child.terminate() }
        if menuLock >= 0 { Darwin.close(menuLock) }
    }

    func startBackend() throws {
        if let child = backendProcess, child.isRunning { return }
        let script = projectURL.appendingPathComponent("wvcore2.py")
        guard FileManager.default.fileExists(atPath: script.path) else {
            throw NSError(domain: "Zhijian", code: 1, userInfo: [NSLocalizedDescriptionKey:
                "找不到知间的本地项目：\(projectURL.path)。请将项目恢复到原位置后重新打开。"])
        }
        let logURL = projectURL.appendingPathComponent("app-launch.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let log = try FileHandle(forWritingTo: logURL)
        log.seekToEndOfFile()
        backendLog = log
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-u", script.path]
        process.currentDirectoryURL = projectURL
        process.standardOutput = log
        process.standardError = log
        try process.run()
        backendProcess = process
    }

    func installAppMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: "知间")
        appMenu.addItem(withTitle: "关于知间", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        let quit = appMenu.addItem(withTitle: "退出知间", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        appItem.submenu = appMenu
        main.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "编辑")
        for (name, selector, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"),
                ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: name, action: NSSelectorFromString(selector), keyEquivalent: key)
        }
        editItem.submenu = edit
        main.addItem(editItem)
        NSApp.mainMenu = main
    }

    func showDashboard() {
        NSApp.setActivationPolicy(.regular)
        if dashboardWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1060, height: 780),
                styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            window.title = "知间 · 聊天关系"
            window.minSize = NSSize(width: 680, height: 520)
            window.isReleasedWhenClosed = false
            window.delegate = self
            let web = WKWebView(frame: window.contentView!.bounds)
            web.autoresizingMask = [.width, .height]
            window.contentView = web
            webView = web
            dashboardWindow = window
            window.center()
        }
        dashboardWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if webView?.url?.host == "127.0.0.1", backendProcess?.isRunning == true { return }
        webView?.loadHTMLString("<body style='background:#111318;color:#e8eaf0;font:16px -apple-system;padding:48px'><h2>知间</h2><p>正在连接本地分析服务…</p></body>", baseURL: nil)
        do { try startBackend() } catch {
            let alert = NSAlert()
            alert.messageText = "知间暂时无法启动"
            alert.informativeText = error.localizedDescription
            alert.runModal()
            return
        }
        dashboardRequest += 1
        waitForDashboard(remaining: 60, requestID: dashboardRequest)
    }

    func waitForDashboard(remaining: Int, requestID: Int) {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:8765/api/state")!)
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            let ready = (response as? HTTPURLResponse)?.statusCode == 200
                && data.flatMap { try? JSONDecoder().decode(AppState.self, from: $0) } != nil
            DispatchQueue.main.async {
                guard let self = self, self.dashboardRequest == requestID else { return }
                if ready {
                    self.webView?.load(URLRequest(url: URL(string: "http://127.0.0.1:8765/")!))
                } else if remaining > 0 {
                    DispatchQueue.main.asyncAfter(deadline: .now()+1) { [weak self] in
                        self?.waitForDashboard(remaining: remaining-1, requestID: requestID)
                    }
                } else {
                    self.webView?.loadHTMLString("<body style='background:#111318;color:#e8eaf0;font:16px -apple-system;padding:48px'><h2>本地服务暂未就绪</h2><p>请确认 Alpha 已开启。重新点击菜单栏的“打开知间”可重试。</p></body>", baseURL: nil)
                }
            }
        }.resume()
    }

    // MARK: 状态加载

    func loadState() {
        if let data = try? Data(contentsOf: stateURL),
           let state = try? JSONDecoder().decode(AppState.self, from: data) {
            apiIssue = state.api_issue
            realtime = state.realtime
            chats = state.chats.filter { (0...100).contains($0.score) }
        } else {
            chats = []
        }
        lastMtime = fileMtime()
    }

    func fileMtime() -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: stateURL.path))?[.modificationDate] as? Date
    }

    func tick() {
        if fileMtime() != lastMtime {
            loadState()
            rebuildMenu()
        }
    }

    // MARK: 菜单

    func pinnedChat() -> Chat? {
        guard let id = pinnedID else { return chats.max(by: { $0.score < $1.score }) }
        return chats.first(where: { $0.id == id }) ?? chats.max(by: { $0.score < $1.score })
    }

    // MARK: 图标

    func relationshipLabel(for chat: Chat) -> String {
        return chat.relationship?.label ?? "待判断"
    }

    func relationshipIcon(for chat: Chat) -> NSImage? {
        let styles: [String: (String, NSColor)] = [
            "family": ("house.fill", .systemGreen),
            "friend": ("person.2.fill", .systemBlue),
            "classmate": ("graduationcap.fill", .systemIndigo),
            "colleague": ("briefcase.fill", .systemOrange),
            "flirting": ("sparkles", .systemPurple),
            "partner": ("heart.fill", .systemPink),
            "acquaintance": ("bubble.left", .systemGray),
            "unknown": ("questionmark.circle", .systemGray),
            "group": ("person.3.fill", .systemTeal),
        ]
        let style = styles[chat.relationship?.type ?? "unknown"] ?? styles["unknown"]!
        guard let symbol = NSImage(systemSymbolName: style.0,
                                   accessibilityDescription: relationshipLabel(for: chat)) else { return nil }
        let icon = symbol.withSymbolConfiguration(NSImage.SymbolConfiguration(paletteColors: [style.1])) ?? symbol
        icon.size = NSSize(width: 18, height: 18)
        icon.isTemplate = false
        return icon
    }

    func scoreColor(_ score: Int) -> NSColor {
        if score >= 50 { return .systemGreen }
        if score >= 0 { return .systemOrange }
        return .systemRed
    }

    func scoreText(_ score: Int) -> String {
        return "\(score)"
    }

    // ICON SLOT: 状态栏图标。槽位说明见 assets/ICON_SLOT.md；
    // 菜单栏实际读取 ~/Library/Application Support/wvbar/icon.png（或 .tiff）。
    func customIconImage() -> NSImage? {
        let base = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/wvbar")
        for name in ["icon.png", "icon.tiff"] {
            let url = base.appendingPathComponent(name)
            if let img = NSImage(contentsOf: url) { return img }
        }
        return nil
    }

    func statusBadge(for chat: Chat) -> NSImage {
        let size = NSSize(width: 18, height: 18)
        let img = NSImage(size: size)
        img.lockFocus()
        let ring = scoreColor(chat.score)

        let rect = NSRect(x: 2, y: 2, width: 14, height: 14)
        if let path = chat.avatar, let av = NSImage(contentsOfFile: path) {
            NSGraphicsContext.current?.saveGraphicsState()
            NSBezierPath(ovalIn: rect).addClip()
            av.draw(in: rect)
            NSGraphicsContext.current?.restoreGraphicsState()
        } else {
            let colors: [NSColor] = [.systemBlue, .systemGreen, .systemOrange,
                                     .systemPurple, .systemPink, .systemTeal]
            let base = colors[Int(UInt(bitPattern: chat.name.hashValue) % UInt(colors.count))]
            base.setFill()
            NSBezierPath(ovalIn: rect).fill()
            let initial = String(chat.name.prefix(1)) as NSString
            let attrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.boldSystemFont(ofSize: 9),
                .foregroundColor: NSColor.white,
            ]
            let sz = initial.size(withAttributes: attrs)
            initial.draw(at: NSPoint(x: (18 - sz.width) / 2, y: (18 - sz.height) / 2),
                         withAttributes: attrs)
        }

        ring.setStroke()
        let ringPath = NSBezierPath(ovalIn: NSRect(x: 1, y: 1, width: 16, height: 16))
        ringPath.lineWidth = 2
        ringPath.stroke()

        img.unlockFocus()
        img.isTemplate = false
        return img
    }

    func heartIcon() -> NSImage {
        let size = NSSize(width: 18, height: 18)
        let img = NSImage(size: size)
        img.lockFocus()
        let path = NSBezierPath()
        path.move(to: NSPoint(x: 9, y: 3))
        path.curve(to: NSPoint(x: 3, y: 9),
                   controlPoint1: NSPoint(x: 6.5, y: 4.5),
                   controlPoint2: NSPoint(x: 3, y: 6))
        path.curve(to: NSPoint(x: 9, y: 15.5),
                   controlPoint1: NSPoint(x: 3, y: 12),
                   controlPoint2: NSPoint(x: 6, y: 14))
        path.curve(to: NSPoint(x: 15, y: 9),
                   controlPoint1: NSPoint(x: 12, y: 14),
                   controlPoint2: NSPoint(x: 15, y: 12))
        path.curve(to: NSPoint(x: 9, y: 3),
                   controlPoint1: NSPoint(x: 15, y: 6),
                   controlPoint2: NSPoint(x: 11.5, y: 4.5))
        path.close()
        NSGradient(starting: .systemPink, ending: .systemPurple)?.draw(in: path, angle: -45)
        img.unlockFocus()
        img.isTemplate = false
        return img
    }

    func trendArrow(for chat: Chat) -> String {
        if let t = chat.recent_trend?.close {
            if t.status == "ready" || t.status == "provisional" {
                return t.symbol ?? "→"
            }
            return "→"
        }
        if let t = chat.trend {
            if t.status == "ready" || t.status == "provisional" {
                return t.symbol ?? "→"
            }
            return "→"
        }
        return "→"
    }

    func trendSummary(for chat: Chat) -> String {
        let recent = chat.recent_trend?.close
        let t = recent ?? chat.trend
        guard let t, t.status == "ready" else {
            return recent == nil ? "趋势样本不足（需要至少 4 个消息位置）" : "近300条采样趋势暂不稳定"
        }
        let source = recent != nil ? "近300条采样" : "历史评分"
        let slope = t.slope_per_100_msgs ?? 0
        var text = "\(source)趋势 \(t.symbol ?? "→") \(String(format: "%.2f", slope)) 分/100条消息 · \(t.label ?? "")"
        if let p = t.prediction, let msgs = p.messages, let score = p.score {
            text += "\n再聊 \(msgs) 条：\(String(format: "%.1f", score))"
            if let low = p.low, let high = p.high {
                text += "（\(String(format: "%.1f", low))–\(String(format: "%.1f", high))）"
            }
        }
        if let d = t.description, !d.isEmpty { text += "\n\(d)" }
        return text
    }

    func updateStatusButton() {
        if let issue = apiIssue {
            statusItem.button?.image = NSImage(systemSymbolName: "exclamationmark.triangle.fill", accessibilityDescription: issue.message)
            let labels = ["insufficient_quota": " 额度不足", "rate_limit": " API限流", "authentication": " 密钥异常"]
            statusItem.button?.title = labels[issue.code] ?? " API异常"
            statusItem.button?.toolTip = issue.message
            return
        }
        guard let chat = pinnedChat() else {
            if let custom = customIconImage() {
                statusItem.button?.image = custom
                statusItem.button?.imagePosition = .imageLeft
            } else {
                statusItem.button?.image = heartIcon()
                statusItem.button?.imagePosition = .imageLeft
            }
            statusItem.button?.title = " –"
            statusItem.button?.toolTip = "知间 · 聊天关系"
            return
        }
        statusItem.button?.image = relationshipIcon(for: chat) ?? statusBadge(for: chat)
        statusItem.button?.imagePosition = .imageLeft
        let arrow = trendArrow(for: chat)
        statusItem.button?.title = " \(scoreText(chat.score))\(arrow)"
        let romantic = chat.dimensions?["romantic"].map(String.init) ?? "—"
        statusItem.button?.toolTip = "\(chat.name) · \(relationshipLabel(for: chat)) · \(scoreText(chat.score)) 分 · 心动 \(romantic)\n\(chat.relationship?.reason ?? chat.reason ?? "")\n\(trendSummary(for: chat))"
    }

    func rebuildMenu() {
        updateStatusButton()

        let menu = NSMenu()
        let sorted = chats.sorted { $0.score > $1.score }

        let header = NSMenuItem(title: "聊天关系（点击置顶）", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        menu.addItem(.separator())
        if let issue = apiIssue {
            let alert = NSMenuItem(title: issue.message, action: #selector(openDashboard), keyEquivalent: "")
            alert.target = self
            alert.image = NSImage(systemSymbolName: "exclamationmark.triangle.fill", accessibilityDescription: "API 提示")
            menu.addItem(alert)
        }
        let live = NSMenuItem(title: realtime?.message ?? "等待实时消息连接", action: nil, keyEquivalent: "")
        live.isEnabled = false
        menu.addItem(live)
        let realtimeOn = realtime?.enabled ?? true
        let rtItem = NSMenuItem(title: realtimeOn ? "实时分析：开启（点击暂停）" : "实时分析：已暂停（点击开启）",
                               action: #selector(toggleRealtime), keyEquivalent: "")
        rtItem.target = self
        menu.addItem(rtItem)
        let ecoMode = realtime?.profile == "eco"
        let modeItem = NSMenuItem(title: ecoMode ? "分析模式：省额度（点击切到高精度）" : "分析模式：高精度（点击切到省额度）",
                                 action: #selector(toggleProfile), keyEquivalent: "")
        modeItem.target = self
        menu.addItem(modeItem)
        menu.addItem(.separator())

        for chat in sorted {
            let arrow = trendArrow(for: chat)
            let item = NSMenuItem(title: "\(chat.name)   \(scoreText(chat.score)) \(arrow)",
                                  action: #selector(pinChat(_:)), keyEquivalent: "")
            let attr = NSMutableAttributedString(
                string: "\(chat.name) · \(relationshipLabel(for: chat))   ",
                attributes: [.font: NSFont.menuFont(ofSize: 13)])
            attr.append(NSAttributedString(
                string: "\(scoreText(chat.score)) \(arrow)",
                attributes: [
                    .font: NSFont.boldSystemFont(ofSize: 13),
                    .foregroundColor: scoreColor(chat.score),
                ]))
            item.attributedTitle = attr
            item.target = self
            item.representedObject = chat.id
            item.image = relationshipIcon(for: chat) ?? avatarImage(for: chat)
            if chat.id == pinnedID {
                item.state = .on
            }
            let romantic = chat.dimensions?["romantic"].map(String.init) ?? "—"
            item.toolTip = "心动指数 \(romantic)\n" + (chat.relationship?.reason ?? chat.reason ?? "") + "\n" + trendSummary(for: chat)
            menu.addItem(item)
        }

        menu.addItem(.separator())

        let refresh = NSMenuItem(title: "立即刷新", action: #selector(refreshNow), keyEquivalent: "r")
        refresh.target = self
        menu.addItem(refresh)

        let dash = NSMenuItem(title: "打开知间…", action: #selector(openDashboard), keyEquivalent: "")
        dash.target = self
        menu.addItem(dash)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "退出", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)

        statusItem.menu = menu
    }

    func avatarImage(for chat: Chat) -> NSImage? {
        if let path = chat.avatar, let img = NSImage(contentsOfFile: path) {
            let sized = NSImage(size: NSSize(width: 20, height: 20))
            sized.lockFocus()
            img.draw(in: NSRect(x: 0, y: 0, width: 20, height: 20))
            sized.unlockFocus()
            return sized
        }
        // 无头像 → 画一个带首字的彩色圆
        let size = NSSize(width: 20, height: 20)
        let img = NSImage(size: size)
        img.lockFocus()
        let colors: [NSColor] = [.systemBlue, .systemGreen, .systemOrange, .systemPurple, .systemPink, .systemTeal]
        let color = colors[Int(UInt(bitPattern: chat.name.hashValue) % UInt(colors.count))]
        color.setFill()
        NSBezierPath(ovalIn: NSRect(x: 1, y: 1, width: 18, height: 18)).fill()
        let initial = String(chat.name.prefix(1)) as NSString
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.boldSystemFont(ofSize: 11),
            .foregroundColor: NSColor.white,
        ]
        let sz = initial.size(withAttributes: attrs)
        initial.draw(at: NSPoint(x: (20 - sz.width) / 2, y: (20 - sz.height) / 2), withAttributes: attrs)
        img.unlockFocus()
        return img
    }

    // MARK: 动作

    @objc func pinChat(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        pinnedID = (pinnedID == id) ? nil : id
        if let id = pinnedID {
            UserDefaults.standard.set(id, forKey: "wvbar.pinnedID")
        } else {
            UserDefaults.standard.removeObject(forKey: "wvbar.pinnedID")
        }
        rebuildMenu()
    }

    func postJSON(_ path: String, _ body: [String: Any]) {
        guard let url = URL(string: "http://127.0.0.1:8765" + path) else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: request) { [weak self] _, _, _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self?.loadState()
                self?.rebuildMenu()
            }
        }.resume()
    }

    @objc func toggleRealtime() {
        let current = realtime?.enabled ?? true
        postJSON("/api/realtime", ["enabled": !current])
    }

    @objc func toggleProfile() {
        let target = (realtime?.profile == "eco") ? "accurate" : "eco"
        postJSON("/api/profile", ["profile": target])
    }

    @objc func refreshNow() {
        loadState()
        rebuildMenu()
    }

    @objc func openDashboard() {
        if packagedApp { showDashboard(); return }
        if let url = URL(string: "http://127.0.0.1:8765") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc func quitApp() {
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
