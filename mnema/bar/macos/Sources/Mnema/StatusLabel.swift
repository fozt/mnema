import AppKit
import SwiftUI

/// The menu bar icon. A custom image because MenuBarExtra tints symbols and text with the menu
/// bar color, while the recording dot and the warning triangle must keep their colors.
struct StatusLabel: View {
    let store: Store

    var body: some View {
        StatusIcon(status: status).equatable()  // redraws only when the icon changes
    }

    private var status: StatusItem {
        if store.outdated {
            return StatusItem(icon: .warning, text: "", tooltip: Strings.outdated)
        }
        if store.engineDown {
            return StatusItem(icon: .warning, text: "", tooltip: Strings.engineDown)
        }
        return store.panel?.status ?? StatusItem(icon: .idle, text: "", tooltip: "")
    }
}

private struct StatusIcon: View, Equatable {
    let status: StatusItem

    var body: some View {
        Image(nsImage: StatusImage.make(status))
            .task(id: status.tooltip) { StatusTooltip.set(status.tooltip) }
    }
}

/// Icon tooltip: `.help` on a MenuBarExtra label never reaches the menu bar button, so it is set
/// on the button itself (NSStatusBarButton, a public class) in the menu bar window.
enum StatusTooltip {
    @MainActor static func set(_ text: String) {
        for window in NSApp.windows {
            if let button = find(in: window.contentView) { button.toolTip = text.isEmpty ? nil : text }
        }
    }

    @MainActor private static func find(in view: NSView?) -> NSStatusBarButton? {
        guard let view else { return nil }
        if let button = view as? NSStatusBarButton { return button }
        return view.subviews.lazy.compactMap { find(in: $0) }.first
    }
}

enum StatusImage {
    private static let gap: CGFloat = 4
    private static let dot: CGFloat = 8

    /// Idle and processing: a template waveform; paused: a template pause sign; recording: a red
    /// dot; warning: an orange triangle (not a dot, so it is not mistaken for recording or the
    /// microphone indicator); text uses the menu bar color.
    static func make(_ status: StatusItem) -> NSImage {
        let font = NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .regular)
        let colored = status.icon == .recording || status.icon == .warning
        let symbol: NSImage? =
            switch status.icon {
            case .recording: nil
            case .warning: symbolImage("exclamationmark.triangle.fill", palette: [.white, .systemOrange])
            case .paused: symbolImage("pause.circle")
            case .idle, .saving: symbolImage("waveform")
            }
        let mark = symbol?.size ?? NSSize(width: dot, height: dot)
        let text = status.text.isEmpty ? nil : status.text
        let textSize = text.map { ($0 as NSString).size(withAttributes: [.font: font]) } ?? .zero
        let height = ceil(max(mark.height, textSize.height))
        let width = ceil(mark.width + (text == nil ? 0 : gap + textSize.width))

        let image = NSImage(size: NSSize(width: width, height: height), flipped: false) { _ in
            let markRect = NSRect(
                x: 0, y: (height - mark.height) / 2, width: mark.width, height: mark.height)
            if let symbol {
                symbol.draw(in: markRect)
            } else {
                NSColor.systemRed.setFill()
                NSBezierPath(ovalIn: markRect).fill()
            }
            if let text {
                // the menu bar color is resolved at draw time: light and dark appearance
                let color: NSColor = colored ? .labelColor : .black
                (text as NSString).draw(
                    at: NSPoint(x: mark.width + gap, y: (height - textSize.height) / 2),
                    withAttributes: [.font: font, .foregroundColor: color])
            }
            return true
        }
        image.isTemplate = !colored
        image.accessibilityDescription = text.map { "\(status.tooltip), \($0)" } ?? status.tooltip
        return image
    }

    /// `palette` colors the symbol layers (glyph, background), not the menu bar color.
    private static func symbolImage(_ name: String, palette: [NSColor] = []) -> NSImage? {
        var config = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
        if !palette.isEmpty { config = config.applying(NSImage.SymbolConfiguration(paletteColors: palette)) }
        return NSImage(systemSymbolName: name, accessibilityDescription: nil)?.withSymbolConfiguration(config)
    }
}
