import Cocoa
import WebKit

/// Un transfert tel que décrit par le serveur local.
struct Transfer {
    let id: String
    let name: String
    let status: String
    let percent: Int
    let current: String
    let attempt: Int
    let files: Int
    let elapsed: Int
    let summary: String

    var isActive: Bool { ["starting", "running", "retrying", "paused"].contains(status) }
}

/// Fenêtre native autour de l'interface web : lance le serveur Python embarqué,
/// affiche la page dans un WKWebView, gère les boîtes de dialogue JavaScript,
/// l'icône de la barre de menus et le mode miniature.
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuDelegate,
    WKUIDelegate, WKNavigationDelegate, WKScriptMessageHandler {

    var window: NSWindow!
    var web: WKWebView!
    var miniPanel: NSPanel!
    var miniWeb: WKWebView!
    var server: Process?
    var buffer = ""

    var baseURL: URL?
    var token = ""

    var statusItem: NSStatusItem!
    let statusMenu = NSMenu()
    var transfers: [Transfer] = []
    var hadActive = false
    var doneTimer: Timer?
    var pollTimer: Timer?
    var miniMenuItem: NSMenuItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        // RSYNCGUI_APPEARANCE=dark|light : force l'apparence (utile pour tester les deux thèmes)
        if let mode = ProcessInfo.processInfo.environment["RSYNCGUI_APPEARANCE"] {
            NSApp.appearance = NSAppearance(named: mode == "dark" ? .darkAqua : .aqua)
        }
        buildMenu()
        buildWindow()
        buildMiniPanel()
        buildStatusItem()
        startServer()
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: fenêtre principale

    func makeWebView() -> WKWebView {
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "app")
        let v = WKWebView(frame: .zero, configuration: config)
        v.uiDelegate = self
        v.navigationDelegate = self
        v.setValue(false, forKey: "drawsBackground")
        return v
    }

    /// Matière translucide du système derrière l'interface web (transparente) : effet « verre ».
    func wrapInEffect(_ view: WKWebView, material: NSVisualEffectView.Material) -> NSVisualEffectView {
        let fx = NSVisualEffectView()
        fx.material = material
        fx.blendingMode = .behindWindow
        fx.state = .active
        view.translatesAutoresizingMaskIntoConstraints = false
        fx.addSubview(view)
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: fx.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: fx.trailingAnchor),
            view.topAnchor.constraint(equalTo: fx.topAnchor),
            view.bottomAnchor.constraint(equalTo: fx.bottomAnchor),
        ])
        return fx
    }

    func buildWindow() {
        web = makeWebView()
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 840),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Rsync GUI"
        window.minSize = NSSize(width: 760, height: 520)
        window.contentView = wrapInEffect(web, material: .underWindowBackground)
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("RsyncGUIMain")
        if !window.setFrameUsingName("RsyncGUIMain") { window.center() }
        window.makeKeyAndOrderFront(nil)
    }

    /// Fermer la fenêtre la range dans la barre de menus : les transferts continuent.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    // MARK: mode miniature

    func buildMiniPanel() {
        miniWeb = makeWebView()
        miniPanel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 320),
            styleMask: [.titled, .closable, .resizable, .utilityWindow],
            backing: .buffered, defer: false)
        miniPanel.title = "Rsync GUI"
        miniPanel.level = .floating
        miniPanel.isFloatingPanel = true
        miniPanel.hidesOnDeactivate = false
        miniPanel.isReleasedWhenClosed = false
        miniPanel.minSize = NSSize(width: 340, height: 160)
        miniPanel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        miniPanel.contentView = wrapInEffect(miniWeb, material: .popover)
        miniPanel.titlebarAppearsTransparent = true
        miniPanel.titlebarSeparatorStyle = .none
        miniPanel.setFrameAutosaveName("RsyncGUIMini")
        if !miniPanel.setFrameUsingName("RsyncGUIMini") {
            if let screen = NSScreen.main {
                let f = screen.visibleFrame
                miniPanel.setFrameOrigin(NSPoint(x: f.maxX - 380, y: f.maxY - 270))
            }
        }
    }

    var inMiniMode: Bool { miniPanel.isVisible }

    func enterMini() {
        window.orderOut(nil)
        miniPanel.orderFrontRegardless()
        miniMenuItem?.title = "Agrandir la fenêtre"
    }

    func expand() {
        miniPanel.orderOut(nil)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        miniMenuItem?.title = "Mode miniature"
    }

    @objc func toggleMini() { inMiniMode ? expand() : enterMini() }
    @objc func showMain() { expand() }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !window.isVisible && !miniPanel.isVisible { expand() }
        return true
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        if message.webView === miniWeb, let body = message.body as? [String: Any],
           let h = (body["h"] as? NSNumber)?.doubleValue {
            fitMini(contentHeight: CGFloat(h))
            return
        }
        switch message.body as? String {
        case "mini": enterMini()
        case "expand": expand()
        default: break
        }
    }

    /// Ajuste la hauteur de la miniature à son contenu, en gardant le bord supérieur en place.
    func fitMini(contentHeight: CGFloat) {
        let maxH = (NSScreen.main?.visibleFrame.height ?? 800) - 80
        let h = min(max(contentHeight, 160), maxH)
        var frame = miniPanel.frame
        let target = miniPanel.frameRect(forContentRect: NSRect(x: 0, y: 0, width: frame.width, height: h))
        guard abs(target.height - frame.height) > 2 else { return }
        frame.origin.y += frame.height - target.height
        frame.size.height = target.height
        miniPanel.setFrame(frame, display: true, animate: false)
    }

    // MARK: barre de menus

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            let img = NSImage(systemSymbolName: "arrow.left.arrow.right.circle", accessibilityDescription: "Rsync GUI")
            img?.isTemplate = true
            button.image = img
            button.imagePosition = .imageLeading
        }
        statusMenu.delegate = self
        if let button = statusItem.button {
            button.target = self
            button.action = #selector(statusClicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
    }

    /// Clic = mode miniature (ou retour à la grande fenêtre) ; clic droit / ctrl-clic = menu détaillé.
    @objc func statusClicked(_ sender: NSStatusBarButton) {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true {
            statusItem.menu = statusMenu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
        } else {
            toggleMini()
        }
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        guard menu === statusMenu else { return }
        menu.removeAllItems()
        let active = transfers.filter { $0.isActive }
        if transfers.isEmpty {
            menu.addItem(disabled("Aucun transfert"))
        } else {
            for t in (active.isEmpty ? Array(transfers.prefix(3)) : active) {
                var line = t.name
                switch t.status {
                case "done": line += " — terminé"
                case "error": line += " — échec"
                case "stopped": line += " — arrêté"
                case "retrying": line += " — reprise (essai \(t.attempt))"
                case "paused": line += " — en pause (\(t.percent)%)"
                case "interrupted": line += " — interrompu"
                default: line += " — \(t.percent)%"
                }
                menu.addItem(disabled(line))
                if t.isActive && !t.current.isEmpty { menu.addItem(disabled("   " + t.current)) }
                if t.isActive {
                    menu.addItem(disabled("   " + (t.summary.isEmpty ? "\(t.files) fichiers · \(format(t.elapsed))" : t.summary)))
                    if t.status == "running" || t.status == "paused" {
                        let paused = t.status == "paused"
                        let item = NSMenuItem(title: paused ? "   ▶ Reprendre" : "   ⏸ Mettre en pause",
                                              action: #selector(toggleTransfer(_:)), keyEquivalent: "")
                        item.target = self
                        item.representedObject = [t.id, paused ? "resume" : "pause"]
                        menu.addItem(item)
                    }
                }
            }
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: "Afficher Rsync GUI", action: #selector(showMain), keyEquivalent: "").target = self
        let mini = menu.addItem(withTitle: inMiniMode ? "Agrandir la fenêtre" : "Mode miniature",
                                action: #selector(toggleMini), keyEquivalent: "")
        mini.target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quitter Rsync GUI", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    }

    @objc func toggleTransfer(_ sender: NSMenuItem) {
        guard let info = sender.representedObject as? [String], info.count == 2, let base = baseURL else { return }
        var req = URLRequest(url: base.appendingPathComponent("api/" + info[1]))
        req.httpMethod = "POST"
        req.setValue(token, forHTTPHeaderField: "X-Token")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["id": info[0]])
        URLSession.shared.dataTask(with: req) { [weak self] _, _, _ in
            DispatchQueue.main.async { self?.poll() }
        }.resume()
    }

    func disabled(_ title: String) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        i.isEnabled = false
        return i
    }

    func format(_ s: Int) -> String {
        s >= 3600 ? "\(s / 3600)h\(String(format: "%02d", s % 3600 / 60))" : "\(s / 60)m\(String(format: "%02d", s % 60))s"
    }

    func startPolling() {
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.poll() }
        poll()
    }

    func poll() {
        guard let base = baseURL else { return }
        var req = URLRequest(url: base.appendingPathComponent("api/state"))
        req.setValue(token, forHTTPHeaderField: "X-Token")
        req.timeoutInterval = 3
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let runs = json["runs"] as? [String: [String: Any]] else { return }
            let list = runs.map { (key, r) in
                Transfer(id: key,
                         name: r["name"] as? String ?? "Transfert",
                         status: r["status"] as? String ?? "",
                         percent: (r["overall"] as? Int).flatMap { $0 >= 0 ? $0 : nil } ?? (r["percent"] as? Int ?? 0),
                         current: r["current"] as? String ?? "",
                         attempt: r["attempt"] as? Int ?? 0,
                         files: r["files"] as? Int ?? 0,
                         elapsed: r["elapsed"] as? Int ?? 0,
                         summary: r["summary"] as? String ?? "")
            }
            DispatchQueue.main.async { self?.apply(list) }
        }.resume()
    }

    func apply(_ list: [Transfer]) {
        transfers = list.sorted { ($0.isActive ? 0 : 1) < ($1.isActive ? 0 : 1) }
        let active = transfers.filter { $0.isActive }
        guard let button = statusItem.button else { return }
        if let first = active.first(where: { $0.status != "paused" }) ?? active.first {
            doneTimer?.invalidate()
            let extra = active.count > 1 ? " ×\(active.count)" : ""
            switch first.status {
            case "retrying": button.title = " ↻\(extra)"
            case "paused": button.title = " ⏸ \(first.percent)%\(extra)"
            default: button.title = " \(first.percent)%\(extra)"
            }
            button.toolTip = first.name + (first.current.isEmpty ? "" : "\n" + first.current)
            hadActive = true
        } else {
            if hadActive {
                hadActive = false
                let failed = transfers.contains { $0.status == "error" }
                button.title = failed ? " ⚠︎" : " ✓"
                doneTimer?.invalidate()
                doneTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: false) { [weak self] _ in
                    self?.statusItem.button?.title = ""
                }
                if !NSApp.isActive { NSApp.requestUserAttention(.informationalRequest) }
            }
            if doneTimer?.isValid != true { button.title = "" }
            button.toolTip = "Rsync GUI — clic : miniature · clic droit : menu"
        }
    }

    // MARK: serveur Python

    func findPython() -> String? {
        let candidates = [
            "/usr/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
        ]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    func startServer() {
        guard let resources = Bundle.main.resourcePath else { return }
        guard let python = findPython() else {
            fail("Python 3 est introuvable. Installez les outils Xcode (xcode-select --install).")
            return
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: python)
        proc.arguments = ["-u", resources + "/app.py"]
        var env = ProcessInfo.processInfo.environment
        env["RSYNCGUI_EMBEDDED"] = "1"
        env["RSYNCGUI_RESOURCES"] = resources
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
        proc.environment = env

        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = FileHandle(forWritingAtPath: logPath()) ?? FileHandle.nullDevice
        out.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.consume(text) }
        }
        do { try proc.run() } catch {
            fail("Impossible de lancer le serveur : \(error.localizedDescription)")
            return
        }
        server = proc
        proc.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                if p.terminationReason == .uncaughtSignal || p.terminationStatus != 0 {
                    self?.fail("Le serveur s'est arrêté de façon inattendue. Journal : \(self?.logPath() ?? "")")
                }
            }
        }
    }

    func logPath() -> String {
        let dir = NSHomeDirectory() + "/.rsync-gui"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let path = dir + "/server.log"
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil)
        }
        return path
    }

    /// Ligne attendue : "RSYNCGUI_URL http://127.0.0.1:PORT/ JETON"
    func consume(_ text: String) {
        buffer += text
        for line in buffer.split(separator: "\n") where line.hasPrefix("RSYNCGUI_URL ") {
            let parts = line.split(separator: " ")
            guard parts.count >= 3, baseURL == nil, let u = URL(string: String(parts[1])) else { continue }
            baseURL = u
            token = String(parts[2])
            web.load(URLRequest(url: u))
            miniWeb.load(URLRequest(url: u.appendingPathComponent("mini")))
            startPolling()
        }
    }

    func fail(_ message: String) {
        let a = NSAlert()
        a.messageText = "Rsync GUI"
        a.informativeText = message
        a.addButton(withTitle: "Quitter")
        a.runModal()
        NSApp.terminate(nil)
    }

    // MARK: cycle de vie

    /// L'app reste dans la barre de menus quand la fenêtre est fermée.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard web?.url != nil else { return .terminateNow }
        let js = "Object.values(state.runs).some(r => ['starting','running','retrying','paused'].includes(r.status))"
        web.evaluateJavaScript(js) { result, _ in
            let busy = (result as? Bool) ?? false
            if !busy { sender.reply(toApplicationShouldTerminate: true); return }
            let a = NSAlert()
            a.messageText = "Des transferts sont en cours"
            a.informativeText = "Quitter les interrompt. Vous pourrez les relancer : rsync reprend là où il s'est arrêté."
            a.addButton(withTitle: "Continuer les transferts")
            a.addButton(withTitle: "Quitter")
            sender.reply(toApplicationShouldTerminate: a.runModal() == .alertSecondButtonReturn)
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        server?.terminationHandler = nil
        server?.terminate()
    }

    // MARK: boîtes de dialogue JavaScript (alert / confirm / prompt)

    func sheetWindow(for webView: WKWebView) -> NSWindow {
        webView === miniWeb ? miniPanel : window
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.beginSheetModal(for: sheetWindow(for: webView)) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Annuler")
        a.beginSheetModal(for: sheetWindow(for: webView)) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert()
        a.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = defaultText ?? ""
        a.accessoryView = field
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Annuler")
        a.window.initialFirstResponder = field
        a.beginSheetModal(for: sheetWindow(for: webView)) {
            completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil)
        }
    }

    // Seule l'interface locale peut s'afficher dans la fenêtre.
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let host = navigationAction.request.url?.host, host != "127.0.0.1" {
            decisionHandler(.cancel)
        } else {
            decisionHandler(.allow)
        }
    }

    // MARK: menus

    @objc func reload() { web.reload(); miniWeb.reload() }

    func buildMenu() {
        let main = NSMenu()
        func item(_ title: String, _ action: Selector?, _ key: String = "",
                  _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            return i
        }
        func submenu(_ title: String, _ items: [NSMenuItem]) {
            let holder = NSMenuItem(); let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            holder.submenu = m; main.addItem(holder)
        }
        submenu("Rsync GUI", [
            item("À propos de Rsync GUI", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            item("Masquer Rsync GUI", #selector(NSApplication.hide(_:)), "h"),
            .separator(),
            item("Quitter Rsync GUI", #selector(NSApplication.terminate(_:)), "q"),
        ])
        submenu("Édition", [
            item("Annuler", Selector(("undo:")), "z"),
            item("Rétablir", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Couper", #selector(NSText.cut(_:)), "x"),
            item("Copier", #selector(NSText.copy(_:)), "c"),
            item("Coller", #selector(NSText.paste(_:)), "v"),
            item("Tout sélectionner", #selector(NSText.selectAll(_:)), "a"),
        ])
        let reloadItem = item("Recharger", #selector(reload), "r"); reloadItem.target = self
        miniMenuItem = item("Mode miniature", #selector(toggleMini), "m", [.command, .shift]); miniMenuItem.target = self
        submenu("Présentation", [reloadItem, miniMenuItem])
        submenu("Fenêtre", [
            item("Réduire", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Fermer", #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
