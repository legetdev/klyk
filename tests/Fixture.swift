// Disposable native app for real MCP input, observation, menus, and dialog verification.
import AppKit

// Provide a real drag source and drop destination with independent result capture.
final class DragBox: NSView, NSDraggingSource {
    var source = false
    var onDrop: ((String) -> Void)?
    override init(frame: NSRect) { super.init(frame:frame); registerForDraggedTypes([.string]); setAccessibilityElement(true); setAccessibilityRole(.button) }
    required init?(coder: NSCoder) { fatalError("Not used") }
    // Paint stable visual targets for screenshots and template matching.
    override func draw(_ dirtyRect: NSRect) {
        (source ? NSColor.systemBlue : NSColor.systemGreen).setFill(); bounds.fill()
        (source ? "Drag sample" : "Drop target").draw(at:NSPoint(x:8,y:14),withAttributes:[.foregroundColor:NSColor.white])
    }
    // Start a native drag carrying only disposable fixture text.
    override func mouseDown(with event: NSEvent) {
        guard source else { return }
        let item=NSPasteboardItem();item.setString("Klyk drag payload",forType:.string)
        let drag=NSDraggingItem(pasteboardWriter:item)
        drag.setDraggingFrame(bounds,contents:NSImage(size:bounds.size))
        beginDraggingSession(with:[drag],event:event,source:self)
    }
    // Advertise the single supported drag operation.
    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation { .copy }
    // Accept fixture text when entering the target.
    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation { source ? [] : .copy }
    // Record successful native drop delivery independently from the driver.
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard !source,let text=sender.draggingPasteboard.string(forType:.string) else { return false }
        onDrop?(text);return true
    }
}

final class FixtureDelegate: NSObject, NSApplicationDelegate, NSTextFieldDelegate {
    var windows: [NSWindow] = []
    var fields: [NSTextField] = []
    var scrollViews: [NSScrollView] = []
    var drops: [String] = []
    var opened = ""
    var clicks = 0
    var selections = 0
    var statePath = ProcessInfo.processInfo.environment["KLYK_FIXTURE_STATE"]!

    // Build two independently addressable windows and native controls with stable labels.
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSWindow.allowsAutomaticWindowTabbing = false
        let menu = NSMenu()
        let appItem = NSMenuItem(); menu.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "Quit Fixture", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let fileItem = NSMenuItem(title: "File", action: nil, keyEquivalent: ""); menu.addItem(fileItem)
        let fileMenu = NSMenu(title: "File"); fileItem.submenu = fileMenu
        let increment = NSMenuItem(title: "Increment", action: #selector(clicked(_:)), keyEquivalent: ""); increment.target = self; fileMenu.addItem(increment)
        let save = NSMenuItem(title: "Save Test File", action: #selector(saveFile(_:)), keyEquivalent: "s"); save.target = self; fileMenu.addItem(save)
        fileMenu.addItem(withTitle:"Close Window",action:#selector(NSWindow.performClose(_:)),keyEquivalent:"w")
        let open = NSMenuItem(title:"Open Test File",action:#selector(openFile(_:)),keyEquivalent:"o");open.target=self;fileMenu.addItem(open)
        let editItem = NSMenuItem(title:"Edit",action:nil,keyEquivalent:"");menu.addItem(editItem)
        let edit = NSMenu(title:"Edit");editItem.submenu=edit
        for (title,action,key) in [("Cut","cut:","x"),("Copy","copy:","c"),("Paste","paste:","v"),("Select All","selectAll:","a")] {
            edit.addItem(withTitle:title,action:NSSelectorFromString(action),keyEquivalent:key)
        }
        NSApp.mainMenu = menu
        for index in 0..<2 {
            let window = NSWindow(contentRect: NSRect(x: 120 + index * 420, y: 160, width: 400, height: 600), styleMask: [.titled,.closable,.resizable], backing: .buffered, defer: false)
            window.title = index == 0 ? "Klyk Fixture A" : "Klyk Fixture B"
            window.isReleasedWhenClosed = false
            let offset = Double(ProcessInfo.processInfo.environment["KLYK_FIXTURE_OFFSET"] ?? "0") ?? 0
            window.setFrameOrigin(NSPoint(x:window.frame.origin.x+offset,y:window.frame.origin.y))
            let content = window.contentView!
            let field = NSTextField(frame: NSRect(x: 20, y: 275, width: 330, height: 28))
            field.stringValue = index == 0 ? "Alpha baseline" : "Beta baseline"
            field.setAccessibilityLabel("Input \(index)"); field.delegate = self; content.addSubview(field); fields.append(field)
            let button = NSButton(title: "Increment \(index)", target: self, action: #selector(clicked(_:)))
            button.frame = NSRect(x: 20, y: 220, width: 160, height: 35); content.addSubview(button)
            for offset in 0..<2 {
                let duplicate = NSButton(title: "Duplicate", target: self, action: #selector(clicked(_:)))
                duplicate.frame = NSRect(x: 20 + offset * 170, y: 170, width: 160, height: 32); content.addSubview(duplicate)
            }
            let popup = NSPopUpButton(frame: NSRect(x: 20, y: 120, width: 150, height: 28))
            popup.addItems(withTitles: ["First", "Second", "Third"]); popup.setAccessibilityLabel("Choice")
            popup.target = self; popup.action = #selector(changed(_:)); content.addSubview(popup)
            let slider = NSSlider(value: 25, minValue: 0, maxValue: 100, target: self, action: #selector(changed(_:)))
            slider.frame = NSRect(x: 20, y: 75, width: 280, height: 24); slider.setAccessibilityLabel("Level"); content.addSubview(slider)
            let label = NSTextField(labelWithString: "Klyk visual anchor \(index)")
            label.frame = NSRect(x:20,y:25,width:330,height:28); content.addSubview(label)
            let context = NSMenu(); let contextItem = NSMenuItem(title:"Increment context",action:#selector(clicked(_:)),keyEquivalent:"")
            contextItem.target=self; context.addItem(contextItem); content.menu=context
            let scroll = NSScrollView(frame: NSRect(x:20,y:420,width:340,height:150))
            scroll.hasVerticalScroller = true
            let text = NSTextView(frame:NSRect(x:0,y:0,width:320,height:2200))
            text.string = (0..<100).map { "Scroll line \($0)" }.joined(separator:"\n")
            text.isEditable = false; text.setAccessibilityLabel("Scrollable content")
            scroll.documentView = text; content.addSubview(scroll); scrollViews.append(scroll)
            let source = DragBox(frame:NSRect(x:20,y:350,width:150,height:45))
            source.setAccessibilityLabel("Drag sample"); source.source = true; content.addSubview(source)
            let target = DragBox(frame:NSRect(x:200,y:350,width:150,height:45))
            target.setAccessibilityLabel("Drop target"); target.onDrop = { [weak self] value in self?.drops.append(value); self?.writeState() }; content.addSubview(target)
            window.makeKeyAndOrderFront(nil); windows.append(window)
        }
        NSApp.activate(ignoringOtherApps: true)
        Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in self?.writeState() }
        writeState()
    }

    // Count actual AppKit action delivery independently from the MCP response.
    @objc func clicked(_ sender: Any?) { clicks += 1; writeState() }
    // Count popup/slider action delivery independently from the input driver.
    @objc func changed(_ sender: Any?) { selections += 1; writeState() }
    // Publish changes from keyboard entry as well as accessibility value setting.
    func controlTextDidChange(_ obj: Notification) { writeState() }
    // Exercise a genuine native save sheet and write only the user's disposable test file.
    @objc func saveFile(_ sender: Any?) {
        let panel = NSSavePanel(); panel.nameFieldStringValue = "saved-fixture.txt"
        panel.directoryURL = URL(fileURLWithPath:statePath).deletingLastPathComponent()
        panel.beginSheetModal(for: NSApp.keyWindow ?? windows[0]) { response in
            if response == .OK, let url = panel.url { try? "Klyk fixture saved".write(to: url, atomically: true, encoding: .utf8) }
        }
    }
    // Read only a disposable file selected through a genuine native open panel.
    @objc func openFile(_ sender: Any?) {
        let panel=NSOpenPanel();panel.directoryURL=URL(fileURLWithPath:statePath).deletingLastPathComponent()
        panel.beginSheetModal(for:NSApp.keyWindow ?? windows[0]) { response in
            if response == .OK,let url=panel.url { self.opened=(try? String(contentsOf:url,encoding:.utf8)) ?? "";self.writeState() }
        }
    }
    // Atomically expose fixture state for independent assertions without inspecting private apps.
    func writeState() {
        let data: [String: Any] = ["pid":ProcessInfo.processInfo.processIdentifier, "clicks":clicks,"opened":opened,"selections":selections,
            "selection":fields.map{field -> String in guard let editor=field.currentEditor() as? NSTextView else { return "none" };return NSStringFromRange(editor.selectedRange())}, "fields":fields.map{$0.stringValue}, "scroll":scrollViews.map{$0.documentVisibleRect.origin.y}, "drops":drops, "windows":windows.map{["title":$0.title,"visible":$0.isVisible,"x":$0.frame.origin.x,"y":$0.frame.origin.y,"width":$0.frame.width,"height":$0.frame.height]}]
        if let encoded = try? JSONSerialization.data(withJSONObject:data,options:[.sortedKeys]) { try? encoded.write(to:URL(fileURLWithPath:statePath),options:.atomic) }
    }
}
let app = NSApplication.shared
let delegate = FixtureDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
