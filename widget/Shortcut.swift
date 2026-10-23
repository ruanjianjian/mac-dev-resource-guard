import Carbon
import AppKit

struct ShortcutChoice {
    let code: UInt32
    let modifiers: UInt32
    let label: String
    static let choices = [
        ShortcutChoice(code: UInt32(kVK_ANSI_S), modifiers: UInt32(controlKey | optionKey | cmdKey), label: "⌃⌥⌘S"),
        ShortcutChoice(code: UInt32(kVK_ANSI_R), modifiers: UInt32(controlKey | optionKey | cmdKey), label: "⌃⌥⌘R"),
        ShortcutChoice(code: UInt32(kVK_ANSI_S), modifiers: UInt32(shiftKey | controlKey | optionKey | cmdKey), label: "⌃⌥⇧⌘S")
    ]
    static func firstAvailable(_ register: (ShortcutChoice) -> Bool) -> ShortcutChoice? {
        choices.first(where: register)
    }
}

final class GlobalShortcut {
    private var hotkey: EventHotKeyRef?
    private var handler: EventHandlerRef?
    var label = "未注册，可从菜单栏打开"
    let show: () -> Void
    init(show: @escaping () -> Void) { self.show = show }
    func register() {
        var type = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let installed = InstallEventHandler(GetApplicationEventTarget(), { _, event, context in
            guard let context, let event else { return OSStatus(eventNotHandledErr) }
            var identity = EventHotKeyID()
            let result = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                           nil, MemoryLayout<EventHotKeyID>.size, nil, &identity)
            guard result == noErr, identity.signature == 0x52475244, identity.id == 1 else { return OSStatus(eventNotHandledErr) }
            Unmanaged<GlobalShortcut>.fromOpaque(context).takeUnretainedValue().show()
            return noErr
        }, 1, &type, Unmanaged.passUnretained(self).toOpaque(), &handler)
        guard installed == noErr else { return }
        let selected = ShortcutChoice.firstAvailable { choice in
            RegisterEventHotKey(choice.code, choice.modifiers, EventHotKeyID(signature: 0x52475244, id: 1),
                                GetApplicationEventTarget(), OptionBits(kEventHotKeyExclusive), &hotkey) == noErr
        }
        if let selected { label = selected.label }
    }
    deinit {
        if let hotkey { UnregisterEventHotKey(hotkey) }
        if let handler { RemoveEventHandler(handler) }
    }
}
