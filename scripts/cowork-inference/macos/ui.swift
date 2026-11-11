import AppKit
import ApplicationServices

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var result: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &result) == .success else {
        return nil
    }
    return result
}

func field(_ element: AXUIElement, _ name: String) -> String {
    guard let value = attribute(element, name) else { return "" }
    return String(describing: value)
}

func descendants(_ element: AXUIElement, depth: Int = 0) -> [AXUIElement] {
    guard depth < 40 else { return [] }
    let children = attribute(element, "AXChildren") as? [AXUIElement] ?? []
    return [element] + children.flatMap { descendants($0, depth: depth + 1) }
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

guard AXIsProcessTrusted() else {
    fail("Accessibility permission is not granted to the automation process")
}
guard let app = NSWorkspace.shared.runningApplications.first(where: {
    $0.bundleURL?.path == "/Applications/Claude.app"
}) else { fail("Claude process is unavailable") }

let application = AXUIElementCreateApplication(app.processIdentifier)
AXUIElementSetMessagingTimeout(application, 2)
let windows = attribute(application, "AXWindows") as? [AXUIElement] ?? []
guard !windows.isEmpty else { fail("Claude window is unavailable") }
let controls = windows.flatMap { descendants($0) }
let arguments = Array(CommandLine.arguments.dropFirst())
let labelAttributes = ["AXTitle", "AXDescription", "AXValue", "AXHelp"]
guard let operation = arguments.first else { fail("Expected snapshot, click, or type") }

func key(_ code: CGKeyCode, flags: CGEventFlags = []) {
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else {
        fail("Could not create keyboard event")
    }
    down.flags = flags
    up.flags = flags
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
}

if arguments.count == 2 && (operation == "key" || operation == "paste") {
    app.activate(options: [.activateIgnoringOtherApps])
    if operation == "paste" {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(arguments[1], forType: .string)
        key(9, flags: .maskCommand)
    } else {
        switch arguments[1] {
        case "go-to-folder": key(5, flags: [.maskCommand, .maskShift])
        case "return": key(36)
        default: fail("Unsupported key")
        }
    }
    exit(0)
}

if operation == "snapshot" {
    let rows = controls.map { element in
        (["AXRole"] + labelAttributes + ["AXEnabled", "AXSelected"]).map {
            field(element, $0)
        }
    }
    do {
        let data = try JSONSerialization.data(withJSONObject: rows)
        FileHandle.standardOutput.write(data)
    } catch {
        fail("Could not encode Accessibility snapshot: \(error)")
    }
    exit(0)
}
guard arguments.count >= 2 else { fail("Expected a control label") }
let label = arguments[1]
let sheets = controls.filter { field($0, "AXRole") == "AXSheet" }
let activeControls = sheets.isEmpty ? controls : sheets.flatMap { descendants($0) }
guard let control = activeControls.first(where: { element in
    labelAttributes.contains(where: { field(element, $0) == label })
}) else { fail("Control unavailable: \(label)") }
guard (attribute(control, "AXEnabled") as? Bool) == true else {
    fail("Control disabled: \(label)")
}
if operation == "click" {
    let result = AXUIElementPerformAction(control, "AXPress" as CFString)
    guard result == .success else { fail("AXPress failed for \(label): \(result.rawValue)") }
    print("clicked")
} else if operation == "type" && arguments.count == 3 {
    AXUIElementSetAttributeValue(control, "AXFocused" as CFString, kCFBooleanTrue)
    let result = AXUIElementSetAttributeValue(control, "AXValue" as CFString, arguments[2] as CFString)
    guard result == .success else { fail("AXValue failed for \(label): \(result.rawValue)") }
    print("typed")
} else {
    fail("Expected snapshot, click, or type with prompt")
}
