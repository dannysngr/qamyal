// Фон окна установочного образа Qamyal.dmg. Рисуется на Mac системной графикой: шрифт SF Pro Rounded,
// цвета значка Qamyal, дуга из точек от значка к папке «Программы». Пишет два PNG — обычный и для Retina;
// build_dmg.sh склеивает их в один TIFF.
//   swiftc -O make_dmg_background.swift -o /tmp/bg && /tmp/bg bg.png bg@2x.png
// Значки в окне (build_dmg.sh): Qamyal — центр (160, 190), «Программы» — (480, 190), размер 112.

import AppKit

let W: CGFloat = 640, H: CGFloat = 400
let indigo = NSColor(srgbRed: 0.31, green: 0.42, blue: 0.94, alpha: 1)    // как в значке
let violet = NSColor(srgbRed: 0.53, green: 0.36, blue: 0.94, alpha: 1)
let ink = NSColor(srgbRed: 0.08, green: 0.10, blue: 0.16, alpha: 1)
let muted = NSColor(srgbRed: 0.40, green: 0.44, blue: 0.53, alpha: 1)

func rounded(_ size: CGFloat, _ weight: NSFont.Weight) -> NSFont {
    let base = NSFont.systemFont(ofSize: size, weight: weight)
    guard let d = base.fontDescriptor.withDesign(.rounded) else { return base }
    return NSFont(descriptor: d, size: size) ?? base
}

// AppKit рисует снизу вверх — пересчитываем привычные координаты «от верха»
func Y(_ top: CGFloat) -> CGFloat { H - top }

func text(_ s: String, _ font: NSFont, _ color: NSColor, centerX: CGFloat, top: CGFloat, kern: CGFloat = 0) {
    let attr: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color, .kern: kern]
    let str = NSAttributedString(string: s, attributes: attr)
    let size = str.size()
    str.draw(at: NSPoint(x: centerX - size.width / 2, y: Y(top) - size.height))
}

func blob(_ center: NSPoint, _ r: CGFloat, _ color: NSColor) {
    let g = NSGradient(colors: [color, color.withAlphaComponent(0)])!
    g.draw(fromCenter: center, radius: 0, toCenter: center, radius: r, options: [])
}

func draw() {
    // фон: мягкий светлый градиент и два размытых пятна цветов значка
    NSGradient(colors: [NSColor(srgbRed: 0.965, green: 0.968, blue: 1.0, alpha: 1),
                        NSColor(srgbRed: 0.985, green: 0.972, blue: 1.0, alpha: 1)])!
        .draw(in: NSRect(x: 0, y: 0, width: W, height: H), angle: -35)
    blob(NSPoint(x: 70, y: Y(40)), 260, indigo.withAlphaComponent(0.16))
    blob(NSPoint(x: 600, y: Y(370)), 280, violet.withAlphaComponent(0.15))

    // заголовок
    text("QAMYAL  ·  ГОВОРИ БЛАГОЕ", rounded(11, .semibold), indigo, centerX: W / 2, top: 34, kern: 2.2)
    let head = NSMutableAttributedString(string: "Перетащите — ", attributes: [.font: rounded(27, .bold), .foregroundColor: ink])
    head.append(NSAttributedString(string: "и готово", attributes: [.font: rounded(27, .bold), .foregroundColor: violet]))
    head.draw(at: NSPoint(x: W / 2 - head.size().width / 2, y: Y(54) - head.size().height))

    // мягкие «подложки» под значками, чтобы они не висели в воздухе
    for x in [160.0, 480.0] {
        let r = NSRect(x: x - 74, y: Y(190) - 74, width: 148, height: 148)
        NSColor.white.withAlphaComponent(0.55).setFill()
        NSBezierPath(roundedRect: r, xRadius: 34, yRadius: 34).fill()
    }

    // дуга из точек: от значка к папке, точки растут и меняют цвет — как звук, который летит
    let p0 = NSPoint(x: 236, y: Y(163)), c = NSPoint(x: 320, y: Y(97)), p1 = NSPoint(x: 400, y: Y(161))
    func bez(_ t: CGFloat) -> NSPoint {
        let a = (1 - t) * (1 - t), b = 2 * (1 - t) * t, d = t * t
        return NSPoint(x: a * p0.x + b * c.x + d * p1.x, y: a * p0.y + b * c.y + d * p1.y)
    }
    let n = 15
    for i in 0..<n {
        let t = CGFloat(i) / CGFloat(n - 1) * 0.9
        let pt = bez(t), r = 2.0 + 2.6 * t
        (indigo.blended(withFraction: t, of: violet) ?? indigo).withAlphaComponent(0.35 + 0.65 * t).setFill()
        NSBezierPath(ovalIn: NSRect(x: pt.x - r, y: pt.y - r, width: 2 * r, height: 2 * r)).fill()
    }
    // наконечник — скруглённый «шеврон» по касательной к дуге
    let tip = bez(1.0), back = bez(0.93)
    let ang = atan2(tip.y - back.y, tip.x - back.x)
    let arrow = NSBezierPath()
    let L: CGFloat = 16, spread: CGFloat = 0.62
    arrow.move(to: NSPoint(x: tip.x - L * cos(ang - spread), y: tip.y - L * sin(ang - spread)))
    arrow.line(to: tip)
    arrow.line(to: NSPoint(x: tip.x - L * cos(ang + spread), y: tip.y - L * sin(ang + spread)))
    arrow.lineWidth = 5
    arrow.lineCapStyle = .round
    arrow.lineJoinStyle = .round
    violet.setStroke()
    arrow.stroke()

    // три шага внизу
    let steps = [("1", "Перетащите"), ("2", "Откройте Qamyal"), ("3", "Он всё сделает сам")]
    let colW: CGFloat = 170, startX = W / 2 - colW
    for (i, s) in steps.enumerated() {
        let cx = startX + CGFloat(i) * colW
        let badge = NSRect(x: cx - 11, y: Y(296) - 22, width: 22, height: 22)
        NSGradient(starting: indigo, ending: violet)!.draw(in: NSBezierPath(ovalIn: badge), angle: -45)
        text(s.0, rounded(12, .bold), .white, centerX: cx, top: 299)
        text(s.1, rounded(13, .semibold), ink, centerX: cx, top: 324)
    }

    // подсказка про первый запуск — тихой строкой
    text("Если macOS не даёт открыть: Настройки → Конфиденциальность и безопасность → «Всё равно открыть»",
         rounded(10.5, .regular), muted, centerX: W / 2, top: 366)
}

func render(scale: CGFloat, to path: String) {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(W * scale), pixelsHigh: Int(H * scale),
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = NSSize(width: W, height: H)                 // 2× — те же точки, вдвое больше пикселей
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    draw()
    NSGraphicsContext.restoreGraphicsState()
    try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: path))
}

let args = CommandLine.arguments
render(scale: 1, to: args.count > 1 ? args[1] : "dmg-background.png")
render(scale: 2, to: args.count > 2 ? args[2] : "dmg-background@2x.png")
