// An Accessibility client for one app: `tree PID` prints its UI elements, `click PID ROLE LABEL LOOKUP_SECONDS`
// clicks the centre of the first matching element (any label if LABEL is empty) with the mouse.
import AppKit

func attribute(_ element: AXUIElement, _ name: String) -> String {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success, let value else { return "" }
    let text = (value as? String) ?? String(describing: value)
    return text.replacingOccurrences(of: "\t", with: " ").replacingOccurrences(of: "\n", with: " ")
}

func elements(_ element: AXUIElement, depth: Int = 0) -> [AXUIElement] {
    var children: CFTypeRef?
    guard depth < 80, AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children) == .success,
          let list = children as? [AXUIElement] else { return [element] }
    return [element] + list.flatMap { elements($0, depth: depth + 1) }
}

let arguments = CommandLine.arguments
guard arguments.count >= 3, let pid = pid_t(arguments[2]) else { fatalError("usage: tree|click PID [ROLE LABEL LOOKUP_SECONDS]") }
guard AXIsProcessTrusted() else { fatalError("This process is not trusted for Accessibility") }
let application = AXUIElementCreateApplication(pid)
let labels = [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute]
func printTree(_ all: [AXUIElement]) {
    for element in all {
        print(([kAXRoleAttribute] + labels).map { attribute(element, $0) }.joined(separator: "\t"))
    }
}
switch arguments[1] {
case "tree":
    printTree(elements(application))
case "click" where arguments.count == 6:
    guard let lookupSeconds = Double(arguments[5]), lookupSeconds.isFinite, lookupSeconds >= 0 else {
        fatalError("LOOKUP_SECONDS must be finite and nonnegative")
    }
    let deadline = ProcessInfo.processInfo.systemUptime + lookupSeconds
    var all: [AXUIElement]
    var target: AXUIElement?
    repeat {
        all = elements(application)
        target = all.first(where: { element in
            attribute(element, kAXRoleAttribute) == arguments[3]
                && (arguments[4].isEmpty || labels.contains { attribute(element, $0) == arguments[4] })
        })
        let remaining = deadline - ProcessInfo.processInfo.systemUptime
        if target != nil || remaining <= 0 { break }
        Thread.sleep(forTimeInterval: min(0.1, remaining))
    } while ProcessInfo.processInfo.systemUptime < deadline
    guard let target else {
        printTree(all)
        // fatalError traps before buffered stdout is flushed.
        fflush(stdout)
        fatalError("No \(arguments[3]) labelled \(arguments[4])")
    }
    var position = CGPoint.zero, size = CGSize.zero
    var value: CFTypeRef?
    AXUIElementCopyAttributeValue(target, kAXPositionAttribute as CFString, &value)
    AXValueGetValue(value as! AXValue, .cgPoint, &position)
    AXUIElementCopyAttributeValue(target, kAXSizeAttribute as CFString, &value)
    AXValueGetValue(value as! AXValue, .cgSize, &size)
    let centre = CGPoint(x: position.x + size.width / 2, y: position.y + size.height / 2)
    // An inactive window takes its first click as activation, so activate the app first.
    NSRunningApplication(processIdentifier: pid)?.activate()
    usleep(500_000)
    for type in [CGEventType.leftMouseDown, .leftMouseUp] {
        CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: centre, mouseButton: .left)?.post(tap: .cghidEventTap)
        usleep(100_000)
    }
default:
    fatalError("usage: tree|click PID [ROLE LABEL LOOKUP_SECONDS]")
}
