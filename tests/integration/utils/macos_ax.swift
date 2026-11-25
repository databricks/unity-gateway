// An Accessibility client for one app: `tree PID` prints its UI elements, `click PID ROLE LABEL
// [WAIT]` activates the app, waits up to WAIT seconds (60 by default) for the first element
// matching ROLE and LABEL (any label if LABEL is empty) and clicks that same element with one
// mouse click — posted only once the element's frame has stopped moving, because a dialog that
// is still animating in reports a moving frame and a mid-flight click misses it. The app's web
// controls accept AXPress without acting on it (run 45506553887: the first onboarding screen
// stayed up after successful presses), so the mouse click is the click.
import AppKit

let labels = [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute]

func attribute(_ element: AXUIElement, _ name: String) -> String {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success, let value else { return "" }
    let text = (value as? String) ?? String(describing: value)
    return text.replacingOccurrences(of: "\t", with: " ").replacingOccurrences(of: "\n", with: " ").replacingOccurrences(of: "\r", with: " ")
}

func row(_ element: AXUIElement) -> String {
    ([kAXRoleAttribute] + labels).map { attribute(element, $0) }.joined(separator: "\t")
}

func report(_ message: String) {
    FileHandle.standardError.write(Data("\(message)\n".utf8))
}

// Every element found, plus the AX calls that failed for any reason other than an unsupported
// attribute. A failed call must never masquerade as an empty tree.
struct Snapshot {
    var elements: [AXUIElement] = []
    var errors: [String] = []
}

func snapshot(_ application: AXUIElement) -> Snapshot {
    var result = Snapshot()
    func walk(_ element: AXUIElement, _ depth: Int) {
        result.elements.append(element)
        guard depth < 80 else { return }
        var children: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children)
        // A leaf element reports no children attribute; that is the only silent case.
        if error == .attributeUnsupported { return }
        guard error == .success, let list = children as? [AXUIElement] else {
            result.errors.append("children of \(attribute(element, kAXRoleAttribute)) at depth \(depth): \(error)")
            return
        }
        for child in list { walk(child, depth + 1) }
    }
    walk(application, 0)
    return result
}

// A CFTypeRef must be type-checked before the forced CF downcast: `as?` cannot fail on a
// CoreFoundation type, so the type ID is compared instead of a conditional cast.
func pointValue(_ element: AXUIElement, _ name: String) -> (point: CGPoint, error: AXError) {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    var point = CGPoint.zero
    if error == .success, let cf = value, CFGetTypeID(cf) == AXValueGetTypeID(),
        AXValueGetValue(cf as! AXValue, .cgPoint, &point) {
        return (point, .success)
    }
    return (point, error == .success ? .failure : error)
}

func sizeValue(_ element: AXUIElement, _ name: String) -> (size: CGSize, error: AXError) {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    var size = CGSize.zero
    if error == .success, let cf = value, CFGetTypeID(cf) == AXValueGetTypeID(),
        AXValueGetValue(cf as! AXValue, .cgSize, &size) {
        return (size, .success)
    }
    return (size, error == .success ? .failure : error)
}

let arguments = CommandLine.arguments
guard arguments.count >= 3, let pid = pid_t(arguments[2]) else { fatalError("usage: tree|click PID [ROLE LABEL [WAIT]]") }
guard AXIsProcessTrusted() else { fatalError("This process is not trusted for Accessibility") }
let application = AXUIElementCreateApplication(pid)
switch arguments[1] {
case "tree":
    let snap = snapshot(application)
    for element in snap.elements { print(row(element)) }
    for error in snap.errors { report("AX error: \(error)") }
case "click" where arguments.count == 5 || arguments.count == 6:
    let wait = arguments.count == 6 ? Double(arguments[5]) ?? 60 : 60
    let deadline = Date().addingTimeInterval(wait)
    var activated = false
    while true {
        // Activate before anything is located: an inactive window takes its first mouse click as
        // activation, and activation can move or rebuild windows, so frames are read at click
        // time rather than in advance.
        activated = activated || NSRunningApplication(processIdentifier: pid)?.activate() == true
        var snap = snapshot(application)
        if let target = snap.elements.first(where: { element in
            attribute(element, kAXRoleAttribute) == arguments[3]
                && (arguments[4].isEmpty || labels.contains { attribute(element, $0) == arguments[4] })
        }) {
            let (position, positionError) = pointValue(target, kAXPositionAttribute)
            let (size, sizeError) = sizeValue(target, kAXSizeAttribute)
            if positionError == .success && sizeError == .success && activated {
                // A frame is only clickable once it has stopped moving: a dialog animating in
                // reports a shifting position, and clicking it mid-flight lands where the button
                // used to be. Two reads that agree mean the element has settled.
                usleep(300_000)
                let (settledPosition, settledPositionError) = pointValue(target, kAXPositionAttribute)
                let (settledSize, settledSizeError) = sizeValue(target, kAXSizeAttribute)
                if settledPositionError == .success, settledSizeError == .success,
                    abs(settledPosition.x - position.x) < 1, abs(settledPosition.y - position.y) < 1,
                    abs(settledSize.width - size.width) < 1, abs(settledSize.height - size.height) < 1 {
                    let centre = CGPoint(
                        x: settledPosition.x + settledSize.width / 2,
                        y: settledPosition.y + settledSize.height / 2
                    )
                    report("clicked \(arguments[3]) '\(arguments[4])' at \(centre.x), \(centre.y)")
                    for type in [CGEventType.leftMouseDown, .leftMouseUp] {
                        CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: centre, mouseButton: .left)?.post(tap: .cghidEventTap)
                        usleep(100_000)
                    }
                    exit(0)
                }
            } else if positionError != .success || sizeError != .success {
                snap.errors.append("frame of \(arguments[3]): position \(positionError.rawValue), size \(sizeError.rawValue)")
            }
        }
        if !activated { snap.errors.append("activation of PID \(pid) failed") }
        if Date() >= deadline {
            report("Accessibility tree at failed click (PID \(pid)):")
            for element in snap.elements { report(row(element)) }
            for error in snap.errors { report("AX error: \(error)") }
            fatalError("No \(arguments[3]) labelled \(arguments[4]) (waited \(wait) seconds)")
        }
        usleep(250_000)
    }
default:
    fatalError("usage: tree|click PID [ROLE LABEL [WAIT]]")
}
