"""Drive the installed VS Code and its bundled Copilot Chat through the visible UI.

VS Code runs as the user would start it, on a private X display, with its DevTools port open so
Playwright can read the screen and click. Each step reads the current screen, takes the one
action a user would take on it, and fails on a screen it doesn't know.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .harness import process_group_options, stop_process

FIRST_LAUNCH_TIMEOUT_S = 90
STEP_TIMEOUT_S = 60
TURN_TIMEOUT_S = 240
CLICK_TIMEOUT_MS = 2000
POLL_S = 0.25
# How long a screen gets to react to an action before the same screen is read as new.
SETTLE_S = 3

# Read in one round trip, so one poll classifies one consistent screen.
_SCREEN = """() => {
  const visible = e => !!e && e.getClientRects().length > 0
    && getComputedStyle(e).visibility !== 'hidden';
  const text = e => (e && e.innerText || '').replace(/\\s+/g, ' ').trim();
  const onboarding = document.querySelector('.onboarding-a-overlay.visible');
  const dialog = [...document.querySelectorAll('.monaco-dialog-box')].find(visible);
  const picker = [...document.querySelectorAll('.chat-model-picker-widget')].find(visible);
  const button = document.querySelector('.interactive-input-part .model-picker-split');
  const confirmation = [...document.querySelectorAll(
    '.chat-confirmation-widget-container, .chat-confirmation-widget, .chat-confirmation-widget2'
  )].filter(visible).pop();
  const response = [...document.querySelectorAll(
    '.interactive-item-container.interactive-response'
  )].pop();
  const welcome = [...document.querySelectorAll('.tabs-container .tab')].find(
    e => text(e).startsWith('Welcome')
  );
  const modelsEditor = [...document.querySelectorAll('.monaco-modal-editor-block')].find(
    e => visible(e) && e.querySelector('.models-widget')
  );
  return {
    workbench: !!document.querySelector('.monaco-workbench'),
    onboarding: onboarding ? text(onboarding) : null,
    dialog: dialog ? text(dialog) : null,
    picker: picker ? text(picker) : null,
    pickerSearch: !!(picker && picker.querySelector('input')),
    pickerQuery: picker && picker.querySelector('input') ? picker.querySelector('input').value : null,
    modelButton: button ? button.getAttribute('aria-label') : null,
    confirmation: confirmation ? text(confirmation) : null,
    response: response
      ? {busy: response.classList.contains('chat-response-loading'), text: text(response)}
      : null,
    responses: document.querySelectorAll('.interactive-item-container.interactive-response').length,
    welcome: !!welcome && visible(welcome),
    byokToast: visible(document.evaluate(
      "//*[normalize-space(text())='Set BYOK utility models']", document, null,
      XPathResult.FIRST_ORDERED_NODE_TYPE, null
    ).singleNodeValue),
    modelsEditor: !!modelsEditor,
  };
}"""


class UnknownScreen(AssertionError):
    pass


def vscode_user_dir(session) -> Path:
    """VS Code's user data folder for the session's home."""
    if os.name == "nt":
        return Path(session.env["APPDATA"]) / "Code"
    return Path(session.env["XDG_CONFIG_HOME"]) / "Code"


class VSCode:
    """One VS Code window on the session's project, closed and reaped on exit."""

    def __init__(self, session, name: str):
        self.session = session
        self.name = name
        self.user_dir = vscode_user_dir(session)
        self.shots = 0
        self.actions: list[str] = []
        self.copilot_chat_ready = False

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        session = self.session
        try:
            self.env = session.env if os.name == "nt" else self._start_display()
            port_file = self.user_dir / "DevToolsActivePort"
            port_file.unlink(missing_ok=True)
            self.log = (session.artifacts / f"{self.name}-code.log").open("w")
            # The app itself, not bin/code: that CLI starts the window detached and exits, out of
            # reach of teardown. The sandbox needs a setuid helper the archive can't ship; GPU is
            # absent on CI.
            app = session.which("code").resolve().parent.parent / (
                "Code.exe" if os.name == "nt" else "code"
            )
            self.code = subprocess.Popen(
                [
                    str(app),
                    "--no-sandbox",
                    "--disable-gpu",
                    "--remote-debugging-port=0",
                    str(session.cwd),
                ],
                cwd=session.cwd,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                **process_group_options(),
            )
            port = self._wait(
                lambda: port_file.is_file() and port_file.read_text().split("\n", 1)[0],
                60,
                "VS Code's DevTools port",
            )
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            self.page = self._wait(self._workbench_page, 60, "the VS Code workbench")
        except BaseException:
            self._stop()
            raise
        return self

    def _start_display(self) -> dict:
        """Start a private X display and return the session environment pointed at it."""
        xvfb = shutil.which("Xvfb", path=self.session.env["PATH"])
        assert xvfb, "Xvfb is not on the session PATH"
        read, write = os.pipe()
        self.display = subprocess.Popen(
            [xvfb, "-displayfd", str(write), "-screen", "0", "1600x1000x24", "-nolisten", "tcp"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(write,),
            **process_group_options(),
        )
        os.close(write)
        with os.fdopen(read) as pipe:
            number = pipe.readline().strip()
        assert number.isdigit(), "Xvfb reported no display"
        return {**self.session.env, "DISPLAY": f":{number}"}

    def __exit__(self, kind, value, traceback):
        try:
            if kind is not None:
                self.shot("failure")
                self.session.record(f"{self.name}-failure-screen.json", self._screen_or_none())
        finally:
            self.session.record(f"{self.name}-actions.json", self.actions)
            self._stop()

    def _stop(self):
        for close in (
            lambda: self.browser.close(),
            lambda: self.playwright.stop(),
        ):
            try:
                close()
            except Exception:  # noqa: BLE001 (teardown continues to reap the processes)
                pass
        for name in ("code", "display"):
            if proc := getattr(self, name, None):
                stop_process(proc)
        if log := getattr(self, "log", None):
            log.close()

    def _workbench_page(self):
        for context in self.browser.contexts:
            for page in context.pages:
                if page.locator(".monaco-workbench").count():
                    return page
        return None

    def _wait(self, check, timeout: float, what: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.code.poll() is not None:
                raise AssertionError(f"VS Code exited ({self.code.returncode}) waiting for {what}")
            if result := check():
                return result
            time.sleep(POLL_S)
        raise AssertionError(f"Timed out after {timeout}s waiting for {what}")

    def _screen_or_none(self):
        try:
            return self.screen()
        except Exception:  # noqa: BLE001 (diagnostics only)
            return None

    def screen(self) -> dict:
        return self.page.evaluate(_SCREEN)

    def shot(self, name: str) -> None:
        self.shots += 1
        path = self.session.artifacts / f"{self.name}-{self.shots:02d}-{name}.png"
        try:
            self.page.screenshot(path=str(path))
        except Exception:  # noqa: BLE001 (evidence is best effort; the assertion decides)
            pass

    def _act(self, description: str, action) -> None:
        self.actions.append(description)
        action()

    def _click(self, description: str, locator) -> None:
        self._act(description, lambda: locator.click(timeout=CLICK_TIMEOUT_MS))

    def finish_first_launch(self) -> None:
        """Answer the first-launch onboarding modal and close the Welcome editor.

        Nothing is clicked before the modal shows: clicks during startup can keep it from finishing.
        """
        page = self.page
        overlay = page.locator(".onboarding-a-overlay.visible")
        self._wait(
            lambda: self.screen()["onboarding"],
            FIRST_LAUNCH_TIMEOUT_S,
            "VS Code's first-launch onboarding (startup did not finish)",
        )
        self.shot("onboarding")
        self._click(
            "onboarding: Continue without Signing In",
            overlay.get_by_role("button", name="Continue without Signing In"),
        )
        self._wait(
            lambda: (
                (screen := self.screen())["onboarding"] is None
                or "Continue without Signing In" not in screen["onboarding"]
            ),
            STEP_TIMEOUT_S,
            "the onboarding's next page",
        )
        if self.screen()["onboarding"]:
            self._click("onboarding: Close", overlay.locator('[aria-label="Close"]').first)
        self._wait(lambda: self.screen()["onboarding"] is None, STEP_TIMEOUT_S, "onboarding closed")
        self._wait(lambda: self.screen()["welcome"], STEP_TIMEOUT_S, "the Welcome editor")
        tab = page.locator(".tabs-container .tab", has_text="Welcome").first
        tab.hover(timeout=CLICK_TIMEOUT_MS)
        self._click("Welcome tab: Close", tab.locator('a.action-label[aria-label^="Close"]'))
        self._wait(
            lambda: not self.screen()["welcome"], STEP_TIMEOUT_S, "the Welcome editor closed"
        )

    def pick_model(self, label: str, *, select: bool = True) -> None:
        """Choose ``label`` in Copilot Chat's model picker, trusting the folder and adding the
        model providers on the way when the picker asks for it.

        With ``select=False`` the model must already be the chat's model once the providers are
        added; reaching the model list fails."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        deadline = time.monotonic() + STEP_TIMEOUT_S
        last, acted = None, 0.0
        while time.monotonic() < deadline:
            screen = self.screen()
            if (
                screen["modelButton"] == f"Models, {label}"
                and screen["picker"] is None
                and not screen["modelsEditor"]
            ):
                self._wait_for_copilot_chat()
                self.shot("model-picked")
                return
            state = self._picker_state(screen, label)
            if (
                not select
                and state.startswith("picker: ")
                and state
                not in (
                    "picker: restricted",
                    "picker: signed out",
                )
            ):
                raise AssertionError(
                    f"Copilot Chat didn't start on {label}: {screen['modelButton']}"
                )
            # While VS Code finishes starting, the picker can close by itself, even between a
            # screen read and a click. A closed picker is a screen of its own: open it once, and
            # again only if it is still closed after it had time to open.
            if state == "picker: no match" and time.monotonic() - acted >= SETTLE_S:
                raise AssertionError(f"The model picker lists no {label}")
            if state == last and (state != "picker closed" or time.monotonic() - acted < SETTLE_S):
                time.sleep(POLL_S)
                continue
            last, acted = state, time.monotonic()
            if state == "picker: no match":
                continue
            try:
                self._picker_action(state, screen, label)
            except PlaywrightTimeoutError:
                if self._picker_state(self.screen(), label) == state:
                    raise
                self.actions.append(f"  (the screen changed before the click: {state})")
        raise AssertionError(f"Timed out picking {label}; last screen {self.screen()}")

    def _picker_action(self, state: str, screen: dict, label: str) -> None:
        page = self.page
        picker = page.locator(".chat-model-picker-widget:visible")
        model_button = page.locator(".interactive-input-part .model-picker-split").first
        row = picker.locator(f'.monaco-list-row[aria-label^="{label},"]').first
        if state == "dialog: trust folder":
            self._click(
                "trust dialog: Trust Folder & Continue",
                page.locator(".monaco-dialog-box:visible").get_by_role(
                    "button", name="Trust Folder & Continue"
                ),
            )
        elif state == "language models editor":
            self._act("Language Models editor: Escape", lambda: page.keyboard.press("Escape"))
            # The button keeps its signed-out label for a moment after the providers load.
            self._wait(
                lambda: (
                    not (s := self.screen())["modelsEditor"]
                    and "sign in" not in (s["modelButton"] or "")
                ),
                STEP_TIMEOUT_S,
                "Copilot Chat to list the added models",
            )

        elif state == "picker closed":
            self._click("model picker: open", model_button)
        elif state == "picker: restricted":
            self._click(
                "model picker: Trust Workspace",
                picker.get_by_role("button", name="Trust Workspace"),
            )
        elif state == "picker: signed out":
            self.shot("picker-signed-out")
            self._click(
                "model picker: Add Models...",
                picker.locator('button[aria-label="Add Models..."]'),
            )
        elif state == "picker: models":
            self._click(
                "model picker: Search Models",
                picker.locator('button[aria-label="Search Models"]'),
            )
        elif state == "picker: searching":
            self._act(f"model picker: type {label}", lambda: page.keyboard.type(label))
        elif state == "picker: row shown":
            self._click(f"model picker: {label}", row)
        else:
            raise UnknownScreen(f"Unknown screen while picking {label}: {screen}")

    def _wait_for_copilot_chat(self) -> None:
        # Copilot Chat shows this toast once it runs on Databricks models. A prompt sent before
        # then goes to VS Code's setup agent, which asks for a GitHub sign-in.
        if self.copilot_chat_ready:
            return
        self._wait(
            lambda: self.screen()["byokToast"],
            STEP_TIMEOUT_S,
            "Copilot Chat to start on the Databricks models",
        )
        self.copilot_chat_ready = True
        self.shot("copilot-chat-ready")

    def assert_not_in_picker(self, label: str) -> None:
        """Search Copilot Chat's model picker for ``label``, require no match, and close it."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        page = self.page
        deadline = time.monotonic() + STEP_TIMEOUT_S
        last, acted = None, 0.0
        while time.monotonic() < deadline:
            screen = self.screen()
            state = self._picker_state(screen, label)
            if state == "picker: no match" and time.monotonic() - acted >= SETTLE_S:
                self.shot(f"picker-without-{label}")
                self._act("model picker: Escape", lambda: page.keyboard.press("Escape"))
                self._wait(lambda: self.screen()["picker"] is None, STEP_TIMEOUT_S, "picker closed")
                return
            if state == "picker: row shown":
                self.shot("picker-unexpected-row")
                raise AssertionError(f"The model picker still lists {label}")
            if state == last and (state != "picker closed" or time.monotonic() - acted < SETTLE_S):
                time.sleep(POLL_S)
                continue
            last, acted = state, time.monotonic()
            try:
                if state == "picker: searching":
                    self._act(f"model picker: type {label}", lambda: page.keyboard.type(label))
                elif state in (
                    "picker closed",
                    "picker: models",
                    "picker: restricted",
                    "picker: signed out",
                    "dialog: trust folder",
                    "language models editor",
                ):
                    # Reopening a profile can show the signed-out picker until Copilot Chat starts.
                    self._picker_action(state, screen, label)
                elif state != "picker: no match":
                    raise UnknownScreen(f"Unknown screen while searching for {label}: {screen}")
            except PlaywrightTimeoutError:
                if self._picker_state(self.screen(), label) == state:
                    raise
                self.actions.append(f"  (the screen changed before the click: {state})")
        raise AssertionError(f"Timed out searching the picker for {label}; last {self.screen()}")

    def quit(self) -> None:
        """Quit VS Code from the keyboard, as File > Exit does, and wait for it to exit."""
        # Windows binds Ctrl+Q to Quick Open View; closing the only window quits there.
        key = "Alt+F4" if os.name == "nt" else "Control+q"
        self._act(key, lambda: self.page.keyboard.press(key))
        try:
            self.code.wait(timeout=STEP_TIMEOUT_S)
        except subprocess.TimeoutExpired as error:
            raise AssertionError(f"VS Code did not exit after {key}") from error

    def _picker_state(self, screen: dict, label: str) -> str:
        if screen["onboarding"] is not None:
            return "onboarding"
        if screen["dialog"] is not None:
            if "Do you trust the authors" in screen["dialog"]:
                return "dialog: trust folder"
            return f"dialog: {screen['dialog'][:200]}"
        if screen["modelsEditor"]:
            return "language models editor"
        text = screen["picker"]
        if text is None:
            return "picker closed"
        if "Trust Workspace" in text:
            return "picker: restricted"
        if "Sign in to see available models" in text:
            return "picker: signed out"
        if screen["pickerSearch"]:
            row = self.page.locator(
                f'.chat-model-picker-widget:visible .monaco-list-row[aria-label^="{label},"]'
            )
            if row.count():
                return "picker: row shown"
            return "picker: no match" if screen["pickerQuery"] == label else "picker: searching"
        return "picker: models"

    def send(self, prompt: str) -> int:
        """Type ``prompt`` into the chat input and send it; returns the number of earlier turns."""
        before = self.screen()["responses"]
        editor = self.page.locator(".interactive-input-part .interactive-input-editor").first
        self._click("chat input: focus", editor)
        self._act("chat input: type prompt", lambda: self.page.keyboard.type(prompt))
        self.shot("prompt")
        self._act("chat input: Enter", lambda: self.page.keyboard.press("Enter"))
        return before

    def wait_for_answer(self, before: int, *, allow: str | None = None) -> str:
        """Wait until the turn after ``before`` finishes and return its response text.

        A tool confirmation is allowed only when its text contains ``allow``; any other
        confirmation, and any dialog, fails the turn."""
        deadline = time.monotonic() + TURN_TIMEOUT_S
        idle = 0
        while time.monotonic() < deadline:
            screen = self.screen()
            if screen["dialog"] is not None:
                raise UnknownScreen(f"Unexpected dialog during the turn: {screen['dialog'][:500]}")
            confirmation = screen["confirmation"]
            if confirmation is not None:
                self.shot("confirmation")
                if allow is None or allow not in confirmation.replace(" ", ""):
                    raise UnknownScreen(f"Unexpected tool confirmation: {confirmation[:500]}")
                widget = self.page.locator(
                    ".chat-confirmation-widget-container:visible, .chat-confirmation-widget:visible, "
                    ".chat-confirmation-widget2:visible"
                ).last
                self._click(
                    f"tool confirmation: Allow ({confirmation[:120]})",
                    widget.get_by_role("button", name=re.compile(r"^Allow")).first,
                )
                self._wait(
                    lambda answered=confirmation: self.screen()["confirmation"] != answered,
                    STEP_TIMEOUT_S,
                    "the confirmation to close",
                )
                continue
            response = screen["response"]
            if screen["responses"] > before and response and not response["busy"]:
                # Busy drops only when the whole turn, every tool round included, is over.
                idle += 1
                if idle >= 3:
                    self.shot("answer")
                    return response["text"]
            else:
                idle = 0
            time.sleep(POLL_S)
        raise AssertionError(f"Timed out waiting for the chat turn; last screen {self.screen()}")


def copilot_chat_version(session) -> str:
    """The Copilot Chat version bundled in the installed VS Code."""
    root = session.which("code").resolve().parent.parent
    # Windows keeps the app under a folder named for the build's commit.
    [package] = [
        *root.glob("resources/app/extensions/copilot/package.json"),
        *root.glob("*/resources/app/extensions/copilot/package.json"),
    ]
    return json.loads(package.read_text())["version"]


def ug_extension_config(session) -> dict:
    """The ug-config.json inside the one installed ug extension."""
    folders = sorted((session.home / ".vscode/extensions").glob("databricks.unity-gateway-*"))
    assert len(folders) == 1, folders
    return json.loads((folders[0] / "ug-config.json").read_text())


def model_label(config: dict, model: str) -> str:
    """The picker label ug gave ``model`` in its extension config."""
    entry = next((entry for entry in config["models"] if entry["id"] == model), None)
    assert entry, (
        f"{model} is not among the extension's models: {[e['id'] for e in config['models']]}"
    )
    return entry["name"]


def installed_extensions(session) -> list[str]:
    result = session.run("--list-extensions", binary=session.which("code"), timeout=120)
    return result.stdout.split()
