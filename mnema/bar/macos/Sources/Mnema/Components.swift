import AppKit
import SwiftUI

/// Panel grid, all multiples of 4: text and icons start at x 16, text after a 16 pt icon at x 40,
/// the right edge is x 344; row highlights span 8 to 352.
enum Metrics {
    static let width: CGFloat = 360
    static let inset: CGFloat = 16  // text and controls from the panel edges
    static let rowInset: CGFloat = 8  // row highlight from the edges; row text is 8 further in
    static let glyph: CGFloat = 16  // icon column; text follows 8 after it
    static let target: CGFloat = 28  // icon hit area
    static let iconInset: CGFloat = 10  // right inset of a row ending in a 28 pt icon
    static let icon: CGFloat = 13  // the More and search icons
    static let small: CGFloat = 18  // a small in-row button and the row that holds it
    static let maxList: CGFloat = 520
    static let minList: CGFloat = 180
}

/// Height of the recordings list: fits the content, at most maxList and the screen edge, at least
/// minList when there are enough recordings; frozen (lock) while searching.
enum ListHeight {
    static func of(content: CGFloat, chrome: CGFloat, screen: CGFloat, lock: CGFloat?) -> CGFloat {
        if let lock { return lock }
        let room = min(Metrics.maxList, screen - chrome - 48)
        return max(min(content, Metrics.minList), min(content, room))
    }
}

/// Pointer hover: view state without @State (see Store).
@MainActor
final class Hover: ObservableObject {
    @Published var on = false
}

/// Measured width of a view (to reserve room for it elsewhere in the layout).
final class Width: ObservableObject {
    @Published var value: CGFloat = 0
}

/// Measured panel heights: everything but the list, and the list's own content.
final class PanelHeights: ObservableObject {
    @Published var chrome: CGFloat = 0
    @Published var content: CGFloat = 0
}

/// Full-width filled button, 32 high: red (Start recording, Stop) or neutral. Custom fill because
/// the system red turns gray in an inactive window.
struct FillButtonStyle: ButtonStyle {
    enum Kind { case red, neutral }
    let kind: Kind

    func makeBody(configuration: Configuration) -> some View {
        FillButton(configuration: configuration, kind: kind)
    }
}

private struct FillButton: View {
    let configuration: ButtonStyleConfiguration
    let kind: FillButtonStyle.Kind
    @Environment(\.isEnabled) private var enabled

    var body: some View {
        configuration.label
            .font(.system(size: 13, weight: .semibold))
            .lineLimit(1)
            .foregroundStyle(foreground)
            .frame(maxWidth: .infinity)
            .frame(height: 32)
            .background(fill, in: .rect(cornerRadius: 8))
            .contentShape(.rect(cornerRadius: 8))
            .opacity(configuration.isPressed ? 0.85 : 1)
    }

    private var foreground: AnyShapeStyle {
        guard enabled else { return AnyShapeStyle(.tertiary) }
        return kind == .red ? AnyShapeStyle(Color.white) : AnyShapeStyle(.primary)
    }

    private var fill: Color {
        enabled && kind == .red ? Color(nsColor: .systemRed) : Color.primary.opacity(0.10)
    }
}

/// Filled button label: icon and text 6 apart.
struct FillLabel: View {
    let title: String
    let symbol: String

    var body: some View {
        HStack(spacing: 6) {
            if !symbol.isEmpty { Image(systemName: symbol).accessibilityHidden(true) }
            Text(title)
        }
    }
}

/// Row highlight on hover and press, like menu rows and Control Center.
struct HoverRowStyle: ButtonStyle {
    var height: CGFloat? = 28

    func makeBody(configuration: Configuration) -> some View {
        HoverRow(configuration: configuration, height: height)
    }
}

private struct HoverRow: View {
    let configuration: ButtonStyleConfiguration
    let height: CGFloat?
    @StateObject private var hover = Hover()
    @Environment(\.isEnabled) private var enabled

    var body: some View {
        configuration.label
            .padding(.horizontal, Metrics.rowInset)
            .frame(maxWidth: .infinity, minHeight: height, alignment: .leading)
            .contentShape(.rect)
            .background(highlight, in: .rect(cornerRadius: 6))
            .opacity(enabled ? 1 : 0.45)
            .onHover { hover.on = $0 }
    }

    private var highlight: Color {
        guard enabled else { return .clear }
        if configuration.isPressed { return .primary.opacity(0.14) }
        return hover.on ? .primary.opacity(0.08) : .clear
    }
}

/// An icon in a 28 pt hit area with the More-style hover fill; `title` is the tooltip and the
/// VoiceOver label.
struct IconButton: View {
    let symbol: String
    let title: String
    var checked = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: Metrics.icon))
                .frame(width: Metrics.target, height: Metrics.target)
        }
        .buttonStyle(HoverFillStyle())
        .foregroundStyle(checked ? Color.accentColor : Color.secondary)
        .help(title)
        .accessibilityLabel(title)
        .accessibilityAddTraits(checked ? .isSelected : [])
    }
}

/// Borderless hover fill: icons and the processing place in the header.
struct HoverFillStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        HoverFill(pressed: configuration.isPressed) { configuration.label }
    }
}

/// 6 pt hover backdrop; `pressed` while pressed. More draws it too (it is a menu, not a button).
struct HoverFill<Label: View>: View {
    var pressed = false
    @ViewBuilder let label: () -> Label
    @StateObject private var hover = Hover()

    var body: some View {
        label()
            .contentShape(.rect)
            .background(
                Color.primary.opacity(pressed ? 0.14 : hover.on ? 0.08 : 0), in: .rect(cornerRadius: 6)
            )
            .onHover { hover.on = $0 }
    }
}

/// Small in-row button (card, notice): neutral fill like Pause, readable in an inactive window;
/// `destructive` makes the title red.
struct SmallButtonStyle: ButtonStyle {
    var destructive = false

    func makeBody(configuration: Configuration) -> some View {
        SmallButton(configuration: configuration, destructive: destructive)
    }
}

private struct SmallButton: View {
    let configuration: ButtonStyleConfiguration
    let destructive: Bool
    @Environment(\.isEnabled) private var enabled

    var body: some View {
        configuration.label
            .font(.system(size: 12, weight: .medium))
            .lineLimit(1)
            .foregroundStyle(foreground)
            .padding(.horizontal, 8)
            .frame(height: Metrics.small)
            .background(
                Color.primary.opacity(configuration.isPressed ? 0.18 : 0.10), in: .rect(cornerRadius: 6)
            )
            .contentShape(.rect(cornerRadius: 6))
            .fixedSize()
    }

    private var foreground: AnyShapeStyle {
        guard enabled else { return AnyShapeStyle(.tertiary) }
        return destructive ? AnyShapeStyle(Color(nsColor: .systemRed)) : AnyShapeStyle(.primary)
    }
}

/// A 26×16 switch with its own fill: the system one is gray in an inactive menu bar window and
/// reads as off.
struct MiniSwitchStyle: ToggleStyle {
    func makeBody(configuration: Configuration) -> some View {
        MiniSwitch(configuration: configuration)
    }
}

private struct MiniSwitch: View {
    let configuration: ToggleStyleConfiguration
    @Environment(\.isEnabled) private var enabled

    var body: some View {
        let on = configuration.isOn
        Button {
            configuration.isOn.toggle()
        } label: {
            Capsule()
                .fill(on ? Color.accentColor : Color.primary.opacity(0.16))
                .frame(width: 26, height: 16)
                .overlay(alignment: on ? .trailing : .leading) {
                    Circle()
                        .fill(.white)
                        .shadow(color: .black.opacity(0.25), radius: 0.5, y: 0.5)
                        .frame(width: 12, height: 12)
                        .padding(2)
                }
                .animation(.easeOut(duration: 0.15), value: on)
                .opacity(enabled ? 1 : 0.45)
                .contentShape(.capsule)
        }
        .buttonStyle(.plain)
        .accessibilityRepresentation { Toggle(isOn: configuration.$isOn) { configuration.label } }
    }
}

/// A 4 pt accent progress bar (the system one is gray in an inactive window). A nil value is
/// indeterminate: a segment runs back and forth, and stands still with Reduce motion.
struct ProgressBar: View {
    let value: Double?
    @Environment(\.accessibilityReduceMotion) private var still

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.10))
                if let value {
                    Capsule()
                        .fill(Color.accentColor)
                        .frame(width: max(4, geo.size.width * min(max(value, 0), 1)))
                } else {
                    TimelineView(.animation(minimumInterval: 1 / 30, paused: still)) { context in
                        Capsule()
                            .fill(Color.accentColor)
                            .frame(width: geo.size.width / 3)
                            .offset(x: Pulse.wave(at: context.date, period: 2.2) * geo.size.width * 2 / 3)
                    }
                }
            }
        }
        .frame(height: 4)
        .accessibilityElement()
        .accessibilityValue(value.map { L("{n}%", ["n": "\(Int($0 * 100))"]) } ?? "")
    }
}

/// The red recording dot pulses from full opacity down to 0.55 (lower reads as off); it stands still
/// when paused and with Reduce motion. TimelineView rather than phaseAnimator: the latter draws
/// nothing in a menu bar window until its first animation frame.
struct RecordingDot: View {
    let paused: Bool
    @Environment(\.accessibilityReduceMotion) private var still

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 30, paused: paused || still)) { context in
            Circle()
                .fill(paused ? Color.secondary : Color(nsColor: .systemRed))
                .frame(width: 8, height: 8)
                .opacity(paused || still ? 1 : Pulse.opacity(at: context.date))
        }
        .frame(width: Metrics.glyph)
        .accessibilityHidden(true)
    }
}

/// Pulse of the recording dot and the running progress segment: 0…1…0 over `period` seconds.
enum Pulse {
    static func wave(at date: Date, period: Double) -> Double {
        let t = date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: period) / period
        return (1 - cos(2 * .pi * t)) / 2
    }

    static func opacity(at date: Date) -> Double { 1 - 0.45 * wave(at: date, period: 1.8) }
}

/// A Section or Language row: icon, title at x 40, a choice menu on the right. The menu shows
/// `value`: while processing it is the recording's language, not the checked choice.
struct FieldRow: View {
    let field: SettingsField
    let symbol: String
    let store: Store

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: symbol)
                .foregroundStyle(.secondary)
                .frame(width: Metrics.glyph)
                .accessibilityHidden(true)
            Text(field.title).lineLimit(1).accessibilityHidden(true)  // VoiceOver reads the label from the menu
            Spacer(minLength: 8)
            ChoiceMenu(
                value: field.value, title: field.title, choices: field.choices.map { choice in
                    MenuChoice(
                        id: choice.key, title: choice.title, checked: choice.checked,
                        enabled: choice.enabled
                    ) { store.send(choice.key) }
                })
        }
        .frame(height: 28)
        .disabled(!field.enabled)
    }
}

/// A choice menu item; id is the action or section key (titles may coincide).
struct MenuChoice: Identifiable {
    let id: String
    let title: String
    let checked: Bool
    var enabled = true
    var divider = false
    let action: () -> Void
}

/// Borderless choice menu: value and chevron, items with checkmarks.
struct ChoiceMenu: View {
    let value: String
    let title: String  // VoiceOver
    var small = false  // 11 pt in the list header, 13 elsewhere
    var maxWidth: CGFloat?
    let choices: [MenuChoice]

    var body: some View {
        Menu {
            ForEach(choices) { choice in
                if choice.divider { Divider() }
                Toggle(choice.title, isOn: Binding(get: { choice.checked }, set: { _ in choice.action() }))
                    .disabled(!choice.enabled)
            }
        } label: {
            HStack(spacing: 4) {
                Text(value)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: maxWidth, alignment: .trailing)
                Image(systemName: "chevron.down")
                    .font(.system(size: small ? 8 : 9, weight: .semibold))
                    .accessibilityHidden(true)
            }
            .font(small ? .subheadline : .body)
            .foregroundStyle(.secondary)
            .frame(height: Metrics.target)
            .contentShape(.rect)
        }
        .menuStyle(.button)
        .buttonStyle(.plain)
        .menuIndicator(.hidden)
        .fixedSize()
        .help(title)
        .accessibilityLabel(title)
        .accessibilityValue(value)
    }
}

/// A 32 pt source toggle icon, filled with the accent color when on.
struct SourceToggle: View {
    let source: Source
    let store: Store

    var body: some View {
        Button {
            store.send(source.key)
        } label: {
            Image(systemName: source.on ? source.symbol : source.symbolOff)
                .font(.system(size: 14, weight: .medium))
                .frame(width: 32, height: 32)
                .contentShape(.circle)
        }
        .buttonStyle(CircleToggleStyle(on: source.on))
        .opacity(source.enabled ? 1 : 0.5)
        .help(source.title)
        .accessibilityLabel(source.title)
        .accessibilityAddTraits(source.on ? .isSelected : [])
        .disabled(!source.enabled)
    }
}

/// A round toggle, like in Control Center.
private struct CircleToggleStyle: ButtonStyle {
    let on: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(on ? Color.white : Color.secondary)
            .background(on ? Color.accentColor : Color.primary.opacity(0.1), in: .circle)
            .opacity(configuration.isPressed ? 0.7 : 1)
    }
}

/// Audio level: the last 6 bars (newest on the right), 28×16.
struct LevelMeter: View {
    let levels: [Double]
    let color: Color
    private static let bars = 6

    var body: some View {
        let shown = Array(levels.suffix(Self.bars))
        let pad = Self.bars - shown.count
        HStack(spacing: 2) {
            ForEach(0..<Self.bars, id: \.self) { i in
                Capsule()
                    .fill(color)
                    .frame(width: 3, height: 3 + 13 * level(shown, i - pad))
            }
        }
        .frame(width: 28, height: 16)
        .animation(.easeOut(duration: 0.12), value: levels)
        .accessibilityHidden(true)
    }

    private func level(_ shown: [Double], _ i: Int) -> Double {
        shown.indices.contains(i) ? min(max(shown[i], 0), 1) : 0
    }
}

/// Orange warning triangle; `help` is its tooltip (without one the icon is decorative).
struct WarnMark: View {
    var help = ""
    var size: CGFloat = 10

    var body: some View {
        Image(systemName: "exclamationmark.triangle.fill")
            .font(.system(size: size))
            .foregroundStyle(.orange)
            .help(help)
            .accessibilityLabel(help)
            .accessibilityHidden(help.isEmpty)
    }
}

/// The window showing the view, so PanelKeys only handles its own window.
struct WindowReader: NSViewRepresentable {
    let keys: PanelKeys

    func makeNSView(context: Context) -> NSView {
        let view = ReaderView()
        view.keys = keys
        return view
    }

    func updateNSView(_ view: NSView, context: Context) {}

    final class ReaderView: NSView {
        weak var keys: PanelKeys?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            MainActor.assumeIsolated { keys?.window = window }
        }
    }
}
