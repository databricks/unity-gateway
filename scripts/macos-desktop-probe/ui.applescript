on elementText(elementRef)
    set pieces to {}
    tell application "System Events"
        repeat with attributeName in {"AXRole", "AXTitle", "AXDescription", "AXValue", "AXHelp"}
            try
                set attributeValue to value of attribute (contents of attributeName) of elementRef
                if attributeValue is not missing value then set end of pieces to attributeValue as text
            end try
        end repeat
    end tell
    set AppleScript's text item delimiters to " | "
    set resultText to pieces as text
    set AppleScript's text item delimiters to ""
    return resultText
end elementText

on hasLabel(elementRef, targetLabel)
    tell application "System Events"
        repeat with attributeName in {"AXTitle", "AXDescription", "AXValue", "AXHelp"}
            try
                if (value of attribute (contents of attributeName) of elementRef) as text is targetLabel then return true
            end try
        end repeat
    end tell
    return false
end hasLabel

on run argv
    set operation to item 1 of argv
    tell application "System Events"
        if not (exists process "Claude") then error "Claude process is unavailable"
        tell process "Claude"
            set frontmost to true
            if (count of windows) is 0 then error "Claude window is unavailable"
            set controls to entire contents of window 1
        end tell
    end tell
    if operation is "snapshot" then
        set lines to {}
        repeat with elementRef in controls
            set end of lines to my elementText(elementRef)
        end repeat
        set AppleScript's text item delimiters to linefeed
        return lines as text
    end if
    set targetLabel to item 2 of argv
    repeat with elementRef in controls
        if my hasLabel(elementRef, targetLabel) then
            tell application "System Events"
                if operation is "click" then
                    perform action "AXPress" of elementRef
                    return "clicked"
                else if operation is "type" then
                    set value of attribute "AXFocused" of elementRef to true
                    keystroke item 3 of argv
                    return "typed"
                end if
            end tell
        end if
    end repeat
    error "Control unavailable: " & targetLabel
end run
