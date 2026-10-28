import Foundation

/// Process recording window state: a slider updates its field, typed time moves the slider, the
/// start never passes the end. Port of mnema/bar/range_pick.py::RangePick.
struct RangePick {
    let length: Double
    private(set) var start = 0.0
    private(set) var end: Double

    init(length: Double) {
        self.length = length
        end = length
    }

    /// `push` (slider): a start past the end drags the end along. While typing (push = false) the
    /// other end stays: "0:5" on the way to "0:52" must not move the end.
    mutating func setStart(_ value: Double, push: Bool = true) {
        let value = min(max(0, value), length)
        if push || value <= end {
            start = value
            end = max(end, value)
        }
    }

    mutating func setEnd(_ value: Double, push: Bool = true) {
        let value = min(max(0, value), length)
        if push || value >= start {
            end = value
            start = min(start, value)
        }
    }

    /// The whole recording is selected (fields show whole seconds: 1:35:00 of 1:35:00.8).
    var whole: Bool { start < 1 && end >= length - 1 }

    func caption(_ ask: RangeAsk) -> String {
        whole ? ask.wholeCaption : ask.partCaption.replacingOccurrences(of: "{}", with: Clock.format(end - start))
    }
}

/// Time in the range window, like mnema.live.setup.fmt_clock / parse_offset.
enum Clock {
    /// Seconds → "h:mm:ss" (always the full form, so the format is obvious).
    static func format(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds > 0 else { return "0:00:00" }
        let total = Int(min(seconds, 1e12))
        return String(format: "%d:%02d:%02d", total / 3600, total % 3600 / 60, total % 60)
    }

    // Russian and English units; a unit word absorbs only letters, so "1час30мин" keeps its minutes
    // (tests/fixtures/offsets-v1.json)
    private static let units =
        #/(?:([0-9]+)\s*(?:ч|час\p{L}*|h))?\s*(?:([0-9]+)\s*(?:м|мин\p{L}*|m|min))?\s*(?:([0-9]+)\s*(?:с|сек\p{L}*|s|sec))?/#

    /// Typed time → seconds: "1:05:30", "1:35" (hours:minutes), "1ч 35м", "1h 35m", "35 min",
    /// "20 sec"; a bare number is minutes ("90" is an hour and a half); a comma acts as a dot.
    /// nil when it is not a time.
    static func parse(_ text: String) -> Double? {
        let v = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            .replacingOccurrences(of: ",", with: ".")
        guard !v.isEmpty else { return nil }
        if let m = v.wholeMatch(of: units), m.1 != nil || m.2 != nil || m.3 != nil {
            guard let h = number(m.1 ?? "0"), let min = number(m.2 ?? "0"), let sec = number(m.3 ?? "0")
            else { return nil }
            return h * 3600 + min * 60 + sec
        }
        let parts = v.replacingOccurrences(of: ".", with: ":").split(separator: ":", omittingEmptySubsequences: false)
        let nums = parts.compactMap(number)
        guard nums.count == parts.count, nums.count <= 3 else { return nil }
        if nums.count == 1 {
            return nums[0] * 60
        }
        let (h, min, sec) = (nums[0], nums[1], nums.count > 2 ? nums[2] : 0)
        guard min < 60, sec < 60 else { return nil }
        return h * 3600 + min * 60 + sec
    }

    /// Digits only ("+5" and "-5" are not times); Double avoids overflow on long numbers.
    private static func number(_ s: Substring) -> Double? {
        !s.isEmpty && s.allSatisfy({ $0.isASCII && $0.isNumber }) ? Double(s) : nil
    }
}
