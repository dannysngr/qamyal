// Qamyal для macOS: окно программы. Внутри — WebKit (как Safari) со страницами интерфейса, а всю работу
// делает тот же Python, что и в Windows: оболочка запускает app/app.py --no-window --parent <PID>,
// ждёт, пока он поднимет сервер (адрес — в ~/Documents/Qamyal/.server.json), и показывает http://127.0.0.1:<порт>.
// Окно закрыли — оболочка завершается; Python замечает это сам (следит за PID), сохраняет идущую запись
// и выходит. Фоновая расшифровка (worker.py) продолжается.
//
// Папка программы (где app/, .venv/, Записи/) — ключ QamyalRoot в Info.plist (его пишет install.sh
// для Mac Дэни), без ключа — ~/Qamyal.
//
// Приложение из образа (build_dmg.sh) везёт программу с собой: Contents/Resources/payload и setup.sh.
// При первом запуске и после обновления (другая CFBundleVersion, чем в ~/Qamyal/.installed-version)
// оно само ставит её в ~/Qamyal, качает Python и библиотеки и показывает ход в окне — без Терминала.
// Сборка: swiftc -O QamyalApp.swift -o Qamyal.app/Contents/MacOS/Qamyal -framework Cocoa -framework WebKit

import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKUIDelegate, WKNavigationDelegate,
                         WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?
    var setup: Process?
    var loaded = false

    let root: URL = {
        if let p = Bundle.main.object(forInfoDictionaryKey: "QamyalRoot") as? String, !p.isEmpty {
            return URL(fileURLWithPath: (p as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Qamyal")
    }()
    // записи — в «Документах» (как sysdeps.records_dir); там же адрес сервера и журнал окна
    var jobs: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents/Qamyal")
    }
    let payload = Bundle.main.resourceURL!.appendingPathComponent("payload")
    var version: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "" }
    var marker: URL { root.appendingPathComponent(".installed-version") }

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 700),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Qamyal"
        window.isReleasedWhenClosed = false                  // при открытом мини-окне основное можно закрыть и вернуть
        window.minSize = NSSize(width: 860, height: 560)
        window.center()
        window.setFrameAutosaveName("QamyalMain")          // размер и место окна запоминаются

        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()                    // localStorage (тема, язык) переживает перезапуск
        cfg.mediaTypesRequiringUserActionForPlayback = []
        cfg.userContentController.add(self, name: "qamyal")  // кнопка «Повторить» на странице подготовки
        web = WKWebView(frame: .zero, configuration: cfg)
        web.uiDelegate = self
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")       // без белой вспышки в тёмной теме
        window.contentView = web
        web.loadHTMLString(waitingPage("Запускаю Qamyal…"), baseURL: nil)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if needsSetup() { runSetup() } else { startServer() }
    }

    // ---------- подготовка: программа из приложения → ~/Qamyal ----------

    func needsSetup() -> Bool {
        guard FileManager.default.fileExists(atPath: payload.path) else { return false }   // сборка install.sh
        let python = root.appendingPathComponent(".venv/bin/python")
        let installed = (try? String(contentsOf: marker, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
        return installed != version || !FileManager.default.isExecutableFile(atPath: python.path)
    }

    func runSetup() {
        let first = !FileManager.default.fileExists(atPath: root.appendingPathComponent(".venv").path)
        web.loadHTMLString(setupPage(first ? "Подготовка Qamyal" : "Обновление Qamyal"), baseURL: nil)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [Bundle.main.resourceURL!.appendingPathComponent("setup.sh").path, payload.path]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        p.environment = env
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        var buffer = ""
        out.fileHandleForReading.readabilityHandler = { h in
            guard let s = String(data: h.availableData, encoding: .utf8), !s.isEmpty else { return }
            buffer += s
            while let nl = buffer.firstIndex(of: "\n") {
                let line = String(buffer[..<nl])
                buffer.removeSubrange(...nl)
                let parts = line.split(separator: " ", maxSplits: 2).map(String.init)
                if parts.first == "STEP", parts.count == 3 {
                    DispatchQueue.main.async { self.js("progress(\(parts[1]), \(self.quote(parts[2])))") }
                }
            }
        }
        p.terminationHandler = { proc in
            DispatchQueue.main.async {
                out.fileHandleForReading.readabilityHandler = nil
                self.setup = nil
                if proc.terminationStatus == 0 {
                    try? self.version.write(to: self.marker, atomically: true, encoding: .utf8)
                    self.web.loadHTMLString(self.waitingPage("Запускаю Qamyal…"), baseURL: nil)
                    self.startServer()
                } else {
                    let why = (try? String(contentsOf: self.root.appendingPathComponent(".setup-error"), encoding: .utf8))?
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    self.js("fail(\(self.quote(why ?? "Подготовка прервалась. Подробности — в ~/Qamyal/setup.log")))")
                }
            }
        }
        do { try p.run(); setup = p } catch {
            js("fail(\(quote("Не удалось начать подготовку: \(error.localizedDescription)")))")
        }
    }

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.body as? String == "retry", setup == nil { runSetup(); return }
        // мини-окно (страница /mini): открыть, «поверх всех окон», показать основное окно
        guard let m = message.body as? [String: Any] else { return }
        if let top = m["top"] as? Bool {
            miniTop = top
            mini?.level = top ? .floating : .normal
        }
        switch m["do"] as? String {
        case "open": openMini()
        case "main":
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        default: break
        }
    }

    // ---------- мини-окно: запись, таймер, «Отметить момент», ход обработки ----------

    var mini: NSWindow?
    var miniTop = false

    func openMini() {
        if let w = mini {
            w.makeKeyAndOrderFront(nil)
            return
        }
        guard let port = serverPort() else { return }
        // размер постоянный — под содержимое (как W×H в pageMini); растянуть и развернуть нельзя
        let size = NSSize(width: 320, height: 160)
        let w = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                         styleMask: [.titled, .closable, .miniaturizable],
                         backing: .buffered, defer: false)
        w.title = "Qamyal"
        w.isReleasedWhenClosed = false
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        cfg.userContentController.add(self, name: "qamyal")
        let v = WKWebView(frame: .zero, configuration: cfg)
        v.uiDelegate = self
        v.navigationDelegate = self
        v.setValue(false, forKey: "drawsBackground")
        w.contentView = v
        // место запоминается; в первый раз — правый нижний угол экрана
        if !w.setFrameUsingName("QamyalMini"), let s = NSScreen.main?.visibleFrame {
            w.setFrameOrigin(NSPoint(x: s.maxX - w.frame.width - 16, y: s.minY + 16))
        }
        w.setContentSize(size)
        w.setFrameAutosaveName("QamyalMini")
        w.level = miniTop ? .floating : .normal
        w.collectionBehavior = [.fullScreenAuxiliary]       // видно и рядом с Zoom во весь экран
        w.delegate = self
        v.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/mini")!))
        w.makeKeyAndOrderFront(nil)
        mini = w
    }

    // закрытое мини-окно не держим: иначе его страница продолжала бы опрашивать программу
    func windowWillClose(_ note: Notification) {
        if let w = note.object as? NSWindow, w === mini { mini = nil }
    }

    func js(_ code: String) { web.evaluateJavaScript(code, completionHandler: nil) }

    func quote(_ s: String) -> String {
        let data = try? JSONSerialization.data(withJSONObject: [s])
        let arr = data.flatMap { String(data: $0, encoding: .utf8) } ?? "[\"\"]"
        return String(arr.dropFirst().dropLast())
    }

    func setupPage(_ title: String) -> String {
        let svg = (try? String(contentsOf: payload.appendingPathComponent("app/web/icon.svg"), encoding: .utf8)) ?? ""
        return """
        <html><head><meta charset="utf-8"><style>
        :root { --text:#141a26; --muted:#677085; --line:#e3e6ec; --accent:#2f6fed; --rec:#e5484d; }
        @media (prefers-color-scheme: dark) { :root { --text:#e6e9ef; --muted:#8e98ab; --line:#242d3c; --accent:#5b8cff; --rec:#ff5c62; } }
        body { font: 14px -apple-system; color: var(--text); background: transparent; height: 92vh; display: grid; place-items: center; margin: 0; }
        .box { width: 380px; text-align: center; }
        .logo svg { width: 76px; height: 76px; }
        h1 { font-size: 22px; margin: 14px 0 6px; }
        .sub { color: var(--muted); margin-bottom: 26px; line-height: 1.45; }
        .bar { height: 6px; border-radius: 3px; background: var(--line); overflow: hidden; }
        .bar i { display: block; height: 100%; width: 2%; background: var(--accent); border-radius: 3px; transition: width .6s; }
        #step { color: var(--muted); margin-top: 10px; min-height: 20px; }
        #err { color: var(--rec); margin-top: 14px; line-height: 1.45; display: none; }
        button { display: none; margin: 16px auto 0; font: 14px -apple-system; padding: 7px 18px; border-radius: 7px;
                 border: 0; background: var(--accent); color: #fff; cursor: pointer; }
        </style></head><body><div class="box">
          <div class="logo">\(svg)</div>
          <h1>\(title)</h1>
          <div class="sub">Это нужно один раз и займёт минуту-другую.<br>Qamyal скачает Python и свои библиотеки (около 100 МБ).</div>
          <div class="bar"><i id="bar"></i></div>
          <div id="step">Начинаю…</div>
          <div id="err"></div>
          <button id="retry" onclick="again()">Повторить</button>
        </div><script>
          function progress(p, t) { bar.style.width = p + '%'; step.textContent = t; err.style.display = 'none'; retry.style.display = 'none'; }
          function fail(t) { err.textContent = t; err.style.display = 'block'; retry.style.display = 'block'; }
          function again() { retry.style.display = 'none'; err.style.display = 'none'; step.textContent = 'Начинаю…';
                             window.webkit.messageHandlers.qamyal.postMessage('retry'); }
        </script></body></html>
        """
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }

    func applicationWillTerminate(_ note: Notification) {
        // SIGINT, а не SIGTERM: Python получит KeyboardInterrupt и в finally сохранит идущую запись
        server?.interrupt()
        setup?.terminate()
    }

    // ---------- Python ----------

    func startServer() {
        let python = root.appendingPathComponent(".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            web.loadHTMLString(waitingPage("Не найден Python в \(root.path)/.venv — переустановите Qamyal"), baseURL: nil)
            return
        }
        try? FileManager.default.createDirectory(at: jobs, withIntermediateDirectories: true)
        let logURL = jobs.appendingPathComponent(".app.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let log = try? FileHandle(forWritingTo: logURL)
        _ = try? log?.seekToEnd()

        let p = Process()
        p.executableURL = python
        p.arguments = ["-X", "utf8", root.appendingPathComponent("app/app.py").path,
                       "--no-window", "--parent", String(getpid())]
        p.currentDirectoryURL = root.appendingPathComponent("app")
        var env = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        // из Dock программа получает короткий PATH — добавляем места, где лежат claude, ffmpeg, Homebrew
        env["PATH"] = ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", env["PATH"] ?? "/usr/bin:/bin"]
            .joined(separator: ":")
        env["PYTHONUNBUFFERED"] = "1"
        env["QAMYAL_BUILD"] = version                       // для обновления изнутри программы (app/update.py)
        env["QAMYAL_APP"] = Bundle.main.bundlePath
        p.environment = env
        if let log = log { p.standardOutput = log; p.standardError = log }
        do { try p.run() } catch {
            web.loadHTMLString(waitingPage("Не удалось запустить Python: \(error.localizedDescription)"), baseURL: nil)
            return
        }
        server = p
        waitForServer(tries: 0)
    }

    func waitForServer(tries: Int) {
        if loaded { return }
        if let port = serverPort() {
            let url = URL(string: "http://127.0.0.1:\(port)/api/jobs")!
            var req = URLRequest(url: url)
            req.timeoutInterval = 2
            URLSession.shared.dataTask(with: req) { _, resp, _ in
                DispatchQueue.main.async {
                    if (resp as? HTTPURLResponse)?.statusCode == 200 {
                        self.loaded = true
                        self.web.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/")!))
                    } else { self.retry(tries) }
                }
            }.resume()
        } else { retry(tries) }
    }

    func retry(_ tries: Int) {
        if let p = server, !p.isRunning, tries > 4 {
            web.loadHTMLString(waitingPage("Программа не запустилась — подробности в Документы/Qamyal/.app.log"), baseURL: nil)
            return
        }
        if tries > 240 { return }                            // 2 минуты: первая загрузка моделей бывает долгой
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.waitForServer(tries: tries + 1) }
    }

    func serverPort() -> Int? {
        guard let data = try? Data(contentsOf: jobs.appendingPathComponent(".server.json")),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return obj["port"] as? Int
    }

    func waitingPage(_ text: String) -> String {
        """
        <html><body style="font:15px -apple-system;display:grid;place-items:center;height:90vh;color:#677085;
        background:transparent">\(text)</body></html>
        """
    }

    // ---------- WebKit: системные окна, файлы, внешние ссылки ----------

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Отмена")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert()
        a.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = defaultText ?? ""
        a.accessoryView = field
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Отмена")
        a.window.initialFirstResponder = field
        completionHandler(a.runModal() == .alertFirstButtonReturn ? field.stringValue : nil)
    }

    // «Загрузить готовую запись» — <input type=file>
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.begin { completionHandler($0 == .OK ? panel.urls : nil) }
    }

    // всё, что не наш локальный сервер, открываем в обычном браузере
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, let host = url.host, host != "127.0.0.1", host != "localhost",
           url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    // ---------- меню: без «Правка» в WebKit не работают ⌘C / ⌘V ----------

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "О приложении Qamyal", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Скрыть Qamyal", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Завершить Qamyal", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Правка")
        edit.addItem(withTitle: "Отменить", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Повторить", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Вырезать", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Копировать", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Вставить", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Выделить всё", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let view = NSMenu(title: "Вид")
        view.addItem(withTitle: "Обновить", action: #selector(reload), keyEquivalent: "r")
        viewItem.submenu = view
        main.addItem(viewItem)

        let winItem = NSMenuItem()
        let win = NSMenu(title: "Окно")
        win.addItem(withTitle: "Свернуть", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Закрыть", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win
        main.addItem(winItem)
        NSApp.mainMenu = main
    }

    @objc func reload() { web.reload() }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
