// The user's approval, asked in this helper's own window.
//
// Why here and not in the chat: the chat may be closed, and Python is not
// trusted to report the user's answer. A dialog owned by this process can
// only be clicked by the user or by a program holding the Accessibility
// grant, and the kernel refuses every action aimed at this helper, so the
// agent's own injected input cannot answer it either.
//
// Defaults are all toward deny:
// * Deny is the default button (Return presses it). Allow has no key
//   equivalent, so it takes a deliberate click.
// * Allow is disabled for the first ``armDelay`` seconds, so a click meant
//   for whatever was under the pointer cannot land on a dialog that just
//   appeared.
// * Nobody answering within ``answerWindow`` is a denial, as is a session
//   where no dialog can be shown (screen locked, not on the console).
// The answer window bounds one question to the user, not a unit of agent
// work.
//
// Limit: a process that already holds Accessibility could click Allow.
// Such a process can already drive the whole desktop, so this dialog is
// not what stands between it and the user.

import AppKit
import Foundation

final class NativePrompter: Prompter {
    static let answerWindow: TimeInterval = 120
    static let armDelay: TimeInterval = 1.5

    func ask(_ prompt: Prompt) -> PromptAnswer {
        precondition(!Thread.isMainThread, "the prompter blocks; call it off the main thread")
        if let why = sessionBlocker(), !why.hasPrefix("secure input") {
            return .deny("unavailable: \(why)")
        }
        var answer: PromptAnswer = .deny("timeout")
        DispatchQueue.main.sync {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = prompt.title
            alert.informativeText = prompt.lines.joined(separator: "\n")
            let deny = alert.addButton(withTitle: "Deny")
            deny.keyEquivalent = "\r"
            let allow = alert.addButton(withTitle: prompt.allowLabel)
            allow.keyEquivalent = ""
            allow.isEnabled = false
            alert.window.level = .modalPanel
            NSApp.activate(ignoringOtherApps: true)

            let enable = Timer(timeInterval: NativePrompter.armDelay, repeats: false) { _ in allow.isEnabled = true }
            let expire = Timer(timeInterval: NativePrompter.answerWindow, repeats: false) { _ in NSApp.abortModal() }
            RunLoop.main.add(enable, forMode: .modalPanel)
            RunLoop.main.add(expire, forMode: .modalPanel)
            let result = alert.runModal()
            enable.invalidate()
            expire.invalidate()
            switch result {
            case .alertSecondButtonReturn: answer = .allow
            case .alertFirstButtonReturn: answer = .deny("user")
            default: answer = .deny("timeout")
            }
        }
        return answer
    }
}
