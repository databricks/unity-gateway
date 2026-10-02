"""A real PTY and terminal screen for driving installed interactive agents.

The screen implements terminal device replies; it never substitutes an agent,
gateway, application function, configuration file, or model response.
"""

from __future__ import annotations

import re
import time
import uuid

from tests.e2e_helpers.terminal import TerminalProcess as SharedTerminalProcess

from .evidence import agent_sessions, assert_no_terminal_api_error


class TerminalProcess(SharedTerminalProcess):
    def evidence(self):
        routing_log = (
            self.session.home
            / ".ucode"
            / ("claude-v2-pty.log" if self.agent == "claude" else "codex-v2-interposer.log")
        )
        return {
            "routing_log": routing_log.read_text() if routing_log.is_file() else None,
            "agent_sessions": agent_sessions(self.session, self.agent)
            if self.agent in ("claude", "codex")
            else {},
        }


class ConfigureTerminal(TerminalProcess):
    def select_agent(self, display):
        prompt = "Select coding agents to configure:"
        self.wait_for(lambda text: prompt in text and self.selected_line(), prompt, timeout=120)
        visited = set()
        selected = False
        while True:
            current = self.selected_line()
            # Retain legacy marker support for runs against older pinned ug releases.
            match = re.search(r"[›❯>]\s*(\[✓\]|\[ \]|☑|✗|●|○)\s*(.+)", current)
            assert match, f"Unrecognized agent checkbox: {current}"
            checked, name = match.groups()
            if name in visited:
                break
            visited.add(name)
            wanted = name.strip() == display
            selected = selected or wanted
            if (checked in {"[✓]", "☑", "●"}) != wanted:
                self.send(" ", f"{'select' if wanted else 'deselect'} {name}")
                self.wait_for(
                    lambda text, before=current: self.selected_line() != before, "checkbox change"
                )
            current = self.selected_line()
            self.send("\x1b[B", "next agent checkbox")
            self.wait_for(lambda text, before=current: self.selected_line() != before, "next agent")
        assert selected, f"{display} was not available in the real agent picker"
        self.send("\r", f"configure only {display}")


class AgentTerminal(TerminalProcess):
    def boot(self, timeout=120):
        """Handle only recognized visible onboarding; unknown screens fail."""
        handled = set()
        ready_since = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read()
            assert not self.ended, f"TUI exited before its prompt:\n{self.visible}"
            text = self.visible
            if "Accessing workspace:" in text and self.session.cwd.name in text:
                self.choose("Accessing workspace:", "Yes, I trust this folder")
                continue
            if "Hooks need review" in text:
                self.choose("Hooks need review", "Trust all and continue")
                continue
            if (
                "Update available" in text
                and "Press enter to continue" in text
                and "update-notice" not in handled
            ):
                self.send("\x1b[B", "move selection off Update now to Skip")
                self.send("\r", "continue with Skip selected")
                handled.add("update-notice")
                continue
            # These are interactions with ordinary UI choices, not pre-written
            # onboarding state. Only the test's disposable project is trusted.
            dialogs = [
                (
                    "theme",
                    "Choose the text style" in text and "Dark mode" in text,
                    "\r",
                ),
                (
                    "security-notes",
                    "Security notes" in text and "Enter to continue" in text,
                    "\r",
                ),
                (
                    "trust-folder",
                    self.session.cwd.name in text
                    and bool(re.search(r"1[.)]\s+Yes, I trust (?:this|the) folder", text)),
                    "\r",
                ),
                (
                    "trust-directory",
                    self.session.cwd.name in text
                    and "trust" in text.lower()
                    and bool(re.search(r"1[.)]\s+Yes, (?:continue|proceed)", text)),
                    "\r",
                ),
            ]
            matched = False
            for label, shown, keys in dialogs:
                if shown:
                    matched = True
                    if label not in handled:
                        self.send(keys, label)
                        handled.add(label)
                    break
            if matched:
                continue
            assert "Select login method:" not in text, (
                "Configured ug launched Claude's account-login flow instead of its gateway session:\n"
                + text
            )
            assert not ("Sign in with ChatGPT" in text and "Provide your own API key" in text), (
                "Configured ug launched Codex's account-login flow instead of its gateway session:\n"
                + text
            )
            title = "Claude Code" if self.agent == "claude" else "Codex"
            if (
                title in text
                and "loading" not in text.lower()
                and re.search(r"(?m)^\s*[❯›>]\s*(?!\d+[.)])", text)
            ):
                ready_since = ready_since or time.monotonic()
                if time.monotonic() - ready_since >= 1:
                    self.actions.append({"reason": "prompt-ready", "screen": text})
                    return
            else:
                ready_since = None
        raise AssertionError(
            f"TUI did not reach a usable prompt within {timeout}s:\n{self.visible}"
        )

    def check_input_and_exit(self):
        marker = "ug-boot-" + uuid.uuid4().hex[:12]
        self.send(marker, "type into the prompt without submitting a model request")
        self.wait_for(lambda text: marker in text, "typed text in the prompt")
        self.send("\x15", "Ctrl-U clears the prompt")
        self.wait_for(lambda text: marker not in text, "cleared prompt")
        self.exit_normally()

    def open_model_picker(self, *, model_visible=None):
        """Open Claude's real model picker, record it, then return to the prompt."""
        self.submit("/model")
        self.wait_for(
            lambda text: "Select model" in text and "Switch between Claude models." in text,
            "the model picker",
            timeout=60,
        )
        if model_visible is not None:
            # Native discovery can finish after the picker shell first renders.
            self.wait_for(model_visible, "a discovered model in the picker", timeout=60)
        screen = self.visible
        self.actions.append({"reason": "model-picker-visible", "screen": screen})
        self.send("\x1b", "close the model picker")
        self.wait_for(
            lambda text: "Select model" not in text,
            "the prompt after closing the model picker",
        )
        return screen

    def wait_for_task(self, task, timeout=180):
        permission_in_progress = False

        def completed(screen):
            nonlocal permission_in_progress
            assert_no_terminal_api_error(screen)
            if "Do you want to proceed?" in screen:
                if permission_in_progress:
                    return False
                # A routed Claude child may locate the fixture from the
                # disposable project root before reading it. Interact with
                # that real permission dialog, but never approve a broader or
                # mutating command just because a model requested it.
                project_root = re.escape(str(self.session.cwd.parent))
                filename = re.escape(task.filename)
                safe_find = re.search(
                    rf'(?m)^\s*find {project_root} -name ["\']{filename}["\'] 2>/dev/null\s*$',
                    screen,
                )
                first_yes = re.search(r"(?m)^\s*[›❯>]\s*1\.\s*Yes\s*$", screen)
                assert self.agent == "claude" and safe_find and first_yes, (
                    "Agent requested an unrecognized tool permission:\n" + screen
                )
                self.send("\r", f"allow read-only search for {task.filename}")
                permission_in_progress = True
                return False
            permission_in_progress = False
            return task.completed(self.session, self.agent)

        self.wait_for(
            completed,
            "a completed assistant answer with the file's value",
            timeout=timeout,
        )
        task.assert_completed(self.session, self.agent)

    def exit_normally(self):
        self.submit("/exit")
        self.finish(timeout=30)
