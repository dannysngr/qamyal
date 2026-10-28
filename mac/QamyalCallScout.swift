// Qamyal для macOS: разведчик созвонов — то же, что app/scout/CallScout.cs в Windows, но через
// специальные возможности macOS (Accessibility). Раз в 2 секунды смотрит окна Zoom, Teams, Телемоста,
// МТС Линка, Битрикс24, Telegram, WhatsApp и пишет:
//   1) что в них изменилось (разведка — по ней распознавание для каждого приложения уточняется);
//   2) кто говорит — по подписям вида «Анна, говорит», «Speaking: Anna» или элементу с именем
//      и состоянием «говорит» — в том же виде, что журнал Skype: speak_start / speak_end.
// Только читает, ничего не нажимает. Нужно разрешение «Универсальный доступ» для Qamyal — macOS спросит сама.
// Запуск: QamyalCallScout <разведка.jsonl> <говорящие.jsonl>     Сборка: swiftc -O QamyalCallScout.swift
import AppKit
import ApplicationServices

let apps = ["zoom.us", "zoom", "microsoft teams", "microsoft teams (work or school)", "teams", "телемост",
            "яндекс телемост", "telemost", "mts link", "мтс линк", "webinar", "bitrix24", "битрикс24",
            "telegram", "whatsapp", "discord", "skype"]
let maxNodes = 6000
let nameThenSpeak = try! NSRegularExpression(
    pattern: #"^(.{2,60}?)[\s,:;—–-]+(сейчас\s+)?(говорит|выступает|is speaking|is talking|speaking|talking)(\s+сейчас|\s+now)?[.!]?$"#,
    options: [.caseInsensitive])
let speakThenName = try! NSRegularExpression(
    pattern: #"^(сейчас говорит|говорит|выступает|активный (докладчик|спикер)|speaking|talking|now speaking|current speaker|active speaker)[\s:—–-]+(.{2,60})$"#,
    options: [.caseInsensitive])
let speakWord = try! NSRegularExpression(pattern: #"^(говорит|выступает|speaking|talking|is speaking|is talking)$"#,
                                         options: [.caseInsensitive])
let selfMark = try! NSRegularExpression(pattern: #"\s*\((вы|you|я|me)\)\s*$"#, options: [.caseInsensitive])

let args = CommandLine.arguments
let logURL = URL(fileURLWithPath: args.count > 1 ? args[1] : "call-scout.jsonl")
let hintsURL: URL? = args.count > 2 ? URL(fileURLWithPath: args[2]) : nil

func append(_ url: URL, _ line: String) {
    let data = (line + "\n").data(using: .utf8)!
    if let h = try? FileHandle(forWritingTo: url) { h.seekToEndOfFile(); h.write(data); try? h.close() }
    else { try? data.write(to: url) }
}
func now() -> String { String(format: "%.3f", Date().timeIntervalSince1970) }
func json(_ obj: [String: Any]) -> String {
    let d = try! JSONSerialization.data(withJSONObject: obj, options: [])
    return String(data: d, encoding: .utf8)!
}
func write(_ kind: String, _ fields: [String: Any] = [:]) {
    var o = fields; o["kind"] = kind; o["t"] = Double(now())!
    append(logURL, json(o))
}

func attr(_ el: AXUIElement, _ name: String) -> String {
    var v: CFTypeRef?
    guard AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success, let s = v as? String else { return "" }
    return s.count > 200 ? String(s.prefix(200)) : s
}
func children(_ el: AXUIElement) -> [AXUIElement] {
    var v: CFTypeRef?
    guard AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &v) == .success else { return [] }
    return (v as? [AXUIElement]) ?? []
}

/// подписи всех элементов окна: роль | id | заголовок | описание | значение | подсказка
func snapshot(_ win: AXUIElement) -> Set<String> {
    var out = Set<String>(), stack = [(win, 0)], n = 0
    while n < maxNodes, let item = stack.popLast() {
        let (el, depth) = item
        n += 1
        let sig = [attr(el, kAXRoleAttribute), attr(el, "AXIdentifier"), attr(el, kAXTitleAttribute),
                   attr(el, kAXDescriptionAttribute), attr(el, kAXValueAttribute), attr(el, kAXHelpAttribute)]
            .joined(separator: "|")
        if sig != "|||||" { out.insert(sig) }
        if depth < 40 { stack += children(el).map { ($0, depth + 1) } }
    }
    return out
}

func firstGroup(_ re: NSRegularExpression, _ s: String, _ group: Int) -> String? {
    let r = NSRange(s.startIndex..., in: s)
    guard let m = re.firstMatch(in: s, range: r), let g = Range(m.range(at: group), in: s) else { return nil }
    return String(s[g]).trimmingCharacters(in: .whitespaces)
}

func speakers(_ snap: Set<String>) -> Set<String> {
    var who = Set<String>()
    for s in snap {
        let f = s.components(separatedBy: "|")
        guard f.count >= 6 else { continue }
        for text in [f[2], f[3], f[4]] where !text.isEmpty {
            if let n = firstGroup(nameThenSpeak, text, 1) ?? firstGroup(speakThenName, text, 3) { who.insert(n) }
        }
        let label = !f[2].isEmpty ? f[2] : f[3]
        if label.count >= 2 && label.count <= 60,
           [f[4], f[5]].contains(where: { speakWord.firstMatch(in: $0, range: NSRange($0.startIndex..., in: $0)) != nil }) {
            who.insert(label)
        }
    }
    return who
}

var introduced = Set<String>(), talking = [String: Set<String>](), last = [String: Set<String>]()

func hint(_ kind: String, _ app: String, _ raw: String) {
    guard let url = hintsURL else { return }
    let r = NSRange(raw.startIndex..., in: raw)
    let isSelf = selfMark.firstMatch(in: raw, range: r) != nil
    let name = selfMark.stringByReplacingMatches(in: raw, range: r, withTemplate: "").trimmingCharacters(in: .whitespaces)
    let uri = app + ":" + name.lowercased()
    let base: [String: Any] = ["uri": uri, "name": name, "self": isSelf, "src": app, "t": Double(now())!]
    if introduced.insert(uri).inserted { var p = base; p["kind"] = "participant"; append(url, json(p)) }
    var e = base; e["kind"] = kind; append(url, json(e))
}

write("started")
// без разрешения «Универсальный доступ» окна чужих программ не видны — просим один раз и ждём
if !AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary) {
    write("error", ["msg": "нет разрешения «Универсальный доступ»"])
}
var tick = 0
while true {
    if AXIsProcessTrusted() {
        if tick % 30 == 0 {
            write("windows", ["list": NSWorkspace.shared.runningApplications
                .filter { $0.activationPolicy == .regular }.compactMap { $0.localizedName }])
        }
        for app in NSWorkspace.shared.runningApplications {
            let name = (app.localizedName ?? "").lowercased()
            guard apps.contains(where: { name.hasPrefix($0) }) else { continue }
            let axApp = AXUIElementCreateApplication(app.processIdentifier)
            var v: CFTypeRef?
            guard AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &v) == .success,
                  let wins = v as? [AXUIElement] else { continue }
            var all = Set<String>()
            for (i, w) in wins.enumerated() {
                let key = name + "#" + String(i)
                let snap = snapshot(w)
                all.formUnion(snap)
                let title = attr(w, kAXTitleAttribute)
                if let before = last[key] {
                    let add = snap.subtracting(before), del = before.subtracting(snap)
                    if !add.isEmpty || !del.isEmpty {
                        write("diff", ["proc": name, "title": title, "add": Array(add), "del": Array(del)])
                    }
                } else {
                    write("snap", ["proc": name, "title": title, "items": Array(snap)])
                }
                last[key] = snap
            }
            let nowSpeaking = speakers(all), before = talking[name] ?? []
            for n in nowSpeaking.subtracting(before) { hint("speak_start", name, n) }
            for n in before.subtracting(nowSpeaking) { hint("speak_end", name, n) }
            talking[name] = nowSpeaking
        }
    }
    tick += 1
    Thread.sleep(forTimeInterval: 2)
}
