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
guard let window = windows.first else { fail("Claude window is unavailable") }
let controls = descendants(window)
let arguments = Array(CommandLine.arguments.dropFirst())
let labelAttributes = ["AXTitle", "AXDescription", "AXValue", "AXHelp"]
guard let operation = arguments.first else { fail("Expected snapshot, click, or type") }

if operation == "snapshot" {
    for element in controls {
        print((["AXRole"] + labelAttributes + ["AXEnabled", "AXSelected"]).map {
            field(element, $0)
        }.joined(separator: " | "))
    }
    exit(0)
}
guard arguments.count >= 2 else { fail("Expected a control label") }
let label = arguments[1]
guard let control = controls.first(where: { element in
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
