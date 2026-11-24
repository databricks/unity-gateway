"""Drive the installed VS Code and its bundled Copilot Chat through the visible UI.

VS Code runs as the user would start it, on a private X display, with its DevTools port open so
Playwright can read the screen and click. Each step reads the current screen, takes the one
action a user would take on it, and fails on a screen it doesn't know.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from .harness import WindowsJob, process_group_options, stop_process

FIRST_LAUNCH_TIMEOUT_S = 90
STEP_TIMEOUT_S = 60
TURN_TIMEOUT_S = 240
CLICK_TIMEOUT_MS = 2000
POLL_S = 0.25
# How long a screen gets to react to an action before the same screen is read as new.
SETTLE_S = 3

# ug's extension asks for this on a profile where Copilot Chat never finished its setup. The toast
# stays up after Copilot Chat is turned on another way, over the chat's model picker.
COPILOT_CHAT_OFF_NOTICE = "Databricks models need Copilot Chat, which is turned off."

# Read in one round trip, so one poll classifies one consistent screen.
_SCREEN = """() => {
  const visible = e => !!e && e.getClientRects().length > 0
    && getComputedStyle(e).visibility !== 'hidden';
  const text = e => (e && e.innerText || '').replace(/\\s+/g, ' ').trim();
  const onboarding = document.querySelector('.onboarding-a-overlay.visible');
  const dialog = [...document.querySelectorAll('.monaco-dialog-box')].find(visible);
  const picker = [...document.querySelectorAll('.chat-model-picker-widget')].find(visible);
  const button = document.querySelector('.interactive-input-part .model-picker-split');
  const target = document.querySelector('.interactive-input-part .chat-session-target-picker-item');
  const tour = [...document.querySelectorAll('.spotlight-overlay .spotlight-callout')].find(visible);
  // A finished tool call renders in a collapsible confirmation widget with nothing to approve.
  const confirmation = [...document.querySelectorAll(
    '.chat-confirmation-widget-container, .chat-confirmation-widget, .chat-confirmation-widget2'
  )].filter(e => visible(e)
    && !e.matches('.chat-confirmation-widget-collapsible')
    && !e.querySelector('.chat-confirmation-widget-collapsible')).pop();
  // The chat list renders only rows in view and reuses row elements, so earlier turns can leave
  // the DOM and DOM order isn't chat order. The current turn is the last request by row index and
  // the response after it.
  const row = e => Number(e.closest('.monaco-list-row')?.dataset.index ?? -1);
  const items = [...document.querySelectorAll(
    '.interactive-item-container.interactive-request, .interactive-item-container.interactive-response'
  )].sort((a, b) => row(a) - row(b));
  const last = items.findLastIndex(e => e.classList.contains('interactive-request'));
  const request = last < 0 ? null : items[last];
  const response = items.slice(last + 1).find(e => e.classList.contains('interactive-response'));
  const welcome = [...document.querySelectorAll('.tabs-container .tab')].find(
    e => text(e).startsWith('Welcome')
  );
  const modelsEditor = [...document.querySelectorAll('.monaco-modal-editor-block')].find(
    e => visible(e) && e.querySelector('.models-widget')
  );
  const notice = [...document.querySelectorAll('.notifications-toasts .notification-list-item')].find(
    e => visible(e) && text(e).includes(NOTICE)
  );
  return {
    workbench: !!document.querySelector('.monaco-workbench'),
    onboarding: onboarding ? text(onboarding) : null,
    dialog: dialog ? text(dialog) : null,
    picker: picker ? text(picker) : null,
    pickerSearch: !!(picker && picker.querySelector('input')),
    pickerQuery: picker && picker.querySelector('input') ? picker.querySelector('input').value : null,
    modelButton: button ? button.getAttribute('aria-label') : null,
    sessionTarget: target ? text(target) : null,
    tour: tour ? text(tour) : null,
    confirmation: confirmation ? text(confirmation) : null,
    request: request ? text(request) : null,
    response: response
      ? {busy: response.classList.contains('chat-response-loading'), text: text(response)}
      : null,
    welcome: !!welcome && visible(welcome),
    signedOutNotice: visible(document.evaluate(
      "//*[normalize-space(text())='Choose how you want to use Copilot.']", document, null,
      XPathResult.FIRST_ORDERED_NODE_TYPE, null
    ).singleNodeValue),
    modelsEditor: !!modelsEditor,
    copilotChatOffNotice: !!notice,
    // An open dropdown blocks clicks everywhere else until it closes.
    dropdown: [...document.querySelectorAll('.context-view-block')].some(visible),
  };
}""".replace("NOTICE", json.dumps(COPILOT_CHAT_OFF_NOTICE))


class UnknownScreen(AssertionError):
    pass


def vscode_user_dir(session) -> Path:
    """VS Code's user data folder for the session's home."""
    if os.name == "nt":
        # Like ug and VS Code, fall back to USERPROFILE when APPDATA is unset.
        appdata = session.env.get("APPDATA") or session.home / "AppData" / "Roaming"
        return Path(appdata) / "Code"
    return Path(session.env["XDG_CONFIG_HOME"]) / "Code"


class VSCode:
    """One VS Code window on the session's project, closed and reaped on exit."""

    def __init__(self, session, name: str):
        self.session = session
        self.name = name
        self.user_dir = vscode_user_dir(session)
        self.shots = 0
        self.actions: list[str] = []
        self.states: list[str] = []
        self.started = time.monotonic()
        self.copilot_session_ready = False
        self.job: WindowsJob | None = None

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
            if os.name == "nt":
                self.job = WindowsJob()
            start = self.job.popen if self.job else subprocess.Popen
            self.code = start(
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
            self.page = self._attach(port)
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
            ready, _, _ = select.select([pipe], [], [], STEP_TIMEOUT_S)
            number = pipe.readline().strip() if ready else ""
        assert number.isdigit(), f"Xvfb reported no display within {STEP_TIMEOUT_S}s"
        return {**self.session.env, "DISPLAY": f":{number}"}

    def __exit__(self, kind, value, traceback):
        try:
            if kind is not None:
                self.shot("failure")
                self.session.record(f"{self.name}-failure-screen.json", self._screen_or_none())
        finally:
            self.session.record(f"{self.name}-actions.json", self.actions)
            self.session.record(f"{self.name}-states.json", self.states)
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
        if job := getattr(self, "job", None):
            job.close()
        if log := getattr(self, "log", None):
            log.close()

    def _attach(self, port: str):
        """Connect Playwright to the window once its page target exists, and return the page.

        A target created while Playwright attaches to the browser can be left out of its pages
        for good, so connecting waits for the window, and a connection that still misses the
        workbench is replaced."""
        endpoint = f"http://127.0.0.1:{port}"
        self._wait(
            lambda: any("workbench" in target["url"] for target in self._page_targets(endpoint)),
            STEP_TIMEOUT_S,
            "the VS Code window",
        )
        deadline = time.monotonic() + STEP_TIMEOUT_S
        while True:
            self.browser = self.playwright.chromium.connect_over_cdp(endpoint)
            try:
                return self._wait(
                    self._workbench_page,
                    max(min(4 * SETTLE_S, deadline - time.monotonic()), POLL_S),
                    "the VS Code workbench",
                )
            except AssertionError as error:
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        f"{error}; DevTools page targets: {self._page_targets(endpoint)}"
                    ) from None
            self.actions.append("  (reconnected: the workbench was not among the pages)")
            self.browser.close()

    @staticmethod
    def _page_targets(endpoint: str) -> list[dict]:
        try:
            with urllib.request.urlopen(f"{endpoint}/json/list", timeout=5) as response:
                targets = json.load(response)
        except (OSError, ValueError):
            return []
        return [
            {"url": target.get("url", ""), "title": target.get("title")}
            for target in targets
            if target.get("type") == "page"
        ]

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

    def _seen(self, state: str, screen: dict) -> None:
        """Record each change of the recognized screen, for diagnosing a failed or slow step."""
        line = f"{time.monotonic() - self.started:7.2f}s {state} | {screen['modelButton']}"
        if not self.states or self.states[-1].split(" ", 1)[1] != line.split(" ", 1)[1]:
            self.states.append(line)

    def _click(self, description: str, locator) -> None:
        def click():
            # A tooltip left by the pointer's last position can cover the next target.
            self.page.mouse.move(0, 0)
            locator.click(timeout=CLICK_TIMEOUT_MS)

        self._act(description, click)

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

        # Each action extends the step, up to a cap for a screen that never settles.
        cap = time.monotonic() + 3 * STEP_TIMEOUT_S
        deadline = time.monotonic() + STEP_TIMEOUT_S
        last, acted, picked_since = None, 0.0, None
        while time.monotonic() < deadline:
            screen = self.screen()
            if (
                screen["modelButton"] == f"Models, {label}"
                and screen["picker"] is None
                and not screen["modelsEditor"]
                and not screen["copilotChatOffNotice"]
                and screen["tour"] is None
            ):
                # ug's extension can switch the model and a second Language Models editor can open
                # just after the providers are added, so the picked screen has to hold.
                self._seen("picked", screen)
                last, picked_since = "picked", picked_since or time.monotonic()
                if time.monotonic() - picked_since >= SETTLE_S:
                    if not self.copilot_session_ready:
                        # Copilot Chat can open the second editor as its session starts, so the
                        # picked screen holds again from then.
                        self._wait_for_copilot_session()
                        picked_since, deadline = None, min(time.monotonic() + STEP_TIMEOUT_S, cap)
                        continue
                    self.shot("model-picked")
                    return
                time.sleep(POLL_S)
                continue
            picked_since = None
            state = self._picker_state(screen, label)
            self._seen(state, screen)
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
            deadline = min(acted + STEP_TIMEOUT_S, cap)
            if state == "picker: no match":
                continue
            try:
                self._picker_action(state, screen, label)
            except PlaywrightTimeoutError:
                if self._picker_state(self.screen(), label) == state:
                    raise
                self.actions.append(f"  (the screen changed before the click: {state})")
            if state == "language models editor":
                # Its action returns once the editor has closed, so an editor on the next read is
                # another one, even when no other screen was read in between.
                last = None
        raise AssertionError(f"Timed out picking {label}; last screen {self.screen()}")

    def _picker_action(self, state: str, screen: dict, label: str) -> None:
        page = self.page
        picker = page.locator(".chat-model-picker-widget:visible")
        model_button = page.locator(".interactive-input-part .model-picker-split").first
        row = picker.locator(f'.monaco-list-row[aria-label^="{label},"]').first
        if state == "tour":
            # An experiment shows this tour to some new profiles only; it blocks every other click.
            self.shot("tour")
            self._click(
                "tour: End Tour",
                page.locator(".spotlight-callout:visible").get_by_role("button", name="End Tour"),
            )
        elif state == "dialog: trust folder":
            self._click(
                "trust dialog: Trust Folder & Continue",
                page.locator(".monaco-dialog-box:visible").get_by_role(
                    "button", name="Trust Folder & Continue"
                ),
            )
        elif state == "notice: Copilot Chat off":
            self.shot("copilot-chat-off-notice")
            self._click(
                "ug notice: Turn On Copilot Chat",
                page.locator(".notifications-toasts .notification-list-item:visible")
                .filter(has_text=COPILOT_CHAT_OFF_NOTICE)
                .get_by_role("button", name="Turn On Copilot Chat"),
            )
            self._wait(
                lambda: not self.screen()["copilotChatOffNotice"],
                STEP_TIMEOUT_S,
                "the Copilot Chat notice to close",
            )
        elif state == "language models editor":
            self._wait(
                self._close_models_editor(), STEP_TIMEOUT_S, "the Language Models editor to close"
            )
            # The button keeps its signed-out label for a moment after the providers load.
            self._wait(
                lambda: "sign in" not in (self.screen()["modelButton"] or ""),
                STEP_TIMEOUT_S,
                "Copilot Chat to list the added models",
            )

        elif state == "picker closed":
            # The native model-name button handles Enter without crossing a hover overlay.
            self._act(
                "model picker: open",
                lambda: model_button.locator(".model-picker-name:visible").press(
                    "Enter", timeout=CLICK_TIMEOUT_MS
                ),
            )
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
            self._search_picker(label)
        elif state == "picker: row shown":
            self._click(f"model picker: {label}", row)
        else:
            raise UnknownScreen(f"Unknown screen while picking {label}: {screen}")

    def _close_models_editor(self):
        """A check that is true once no Language Models editor is open, clicking Close on each.

        "Add Models..." and ug's "Turn On Copilot Chat" each open one, so another can be there,
        or open, right after one closes."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        # Its own close button: Escape only closes it while it has focus.
        close = self.page.locator('.monaco-modal-editor-block:visible [aria-label^="Close"]').first
        clicked = 0.0

        def closed() -> bool:
            nonlocal clicked
            if not self.screen()["modelsEditor"]:
                return True
            if time.monotonic() - clicked >= SETTLE_S:
                clicked = time.monotonic()
                try:
                    self._click("Language Models editor: Close", close)
                except PlaywrightTimeoutError:
                    self.actions.append("  (the Language Models editor closed before the click)")
            return False

        return closed

    def _search_picker(self, label: str) -> None:
        # Keys go to the search box itself, not to whatever has focus: a picker that closes
        # itself mid-search hands focus to the chat input, and the rest would land in the prompt.
        search = self.page.locator(".chat-model-picker-widget:visible input").first

        def type_label():
            search.press("ControlOrMeta+a", timeout=CLICK_TIMEOUT_MS)
            for key in label:
                search.press("Space" if key == " " else key, timeout=CLICK_TIMEOUT_MS)

        self._act(f"model picker: type {label}", type_label)

    def _wait_for_copilot_session(self) -> None:
        """Require the chat to be a Copilot session that doesn't ask for a GitHub sign-in."""
        if self.copilot_session_ready:
            return
        screen = self._wait(
            lambda: (screen := self.screen())["sessionTarget"] and screen,
            STEP_TIMEOUT_S,
            "the chat's session type",
        )
        target = screen["sessionTarget"]
        assert target == "Copilot", f"The chat opened a {target!r} session, not a Copilot session"
        if screen["signedOutNotice"]:
            self.shot("copilot-session-signed-out")
            raise AssertionError("The Copilot session asks for a sign-in on Databricks models")
        self.copilot_session_ready = True

    def assert_not_in_picker(self, label: str) -> None:
        """Search Copilot Chat's model picker for ``label``, require no match, and close it."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        page = self.page
        # Each action extends the step, up to a cap for a screen that never settles.
        cap = time.monotonic() + 3 * STEP_TIMEOUT_S
        deadline = time.monotonic() + STEP_TIMEOUT_S
        last, acted = None, 0.0
        while time.monotonic() < deadline:
            screen = self.screen()
            state = self._picker_state(screen, label)
            self._seen(state, screen)
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
            deadline = min(acted + STEP_TIMEOUT_S, cap)
            try:
                if state == "picker: searching":
                    self._search_picker(label)
                elif state in (
                    "picker closed",
                    "picker: models",
                    "picker: restricted",
                    "picker: signed out",
                    "dialog: trust folder",
                    "notice: Copilot Chat off",
                    "language models editor",
                    "tour",
                ):
                    # Reopening a profile can show the signed-out picker until Copilot Chat starts.
                    self._picker_action(state, screen, label)
                elif state != "picker: no match":
                    raise UnknownScreen(f"Unknown screen while searching for {label}: {screen}")
            except PlaywrightTimeoutError:
                if self._picker_state(self.screen(), label) == state:
                    raise
                self.actions.append(f"  (the screen changed before the click: {state})")
            if state == "language models editor":
                # As in pick_model: the next editor seen is another one.
                last = None
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
        # Its other processes can outlive the main one for a moment, still holding the user
        # data folder; the window opened next shares that folder.
        if self.job and not self.job.wait_until_empty(STEP_TIMEOUT_S):
            self.actions.append(
                f"  ({self.job.active_processes()} VS Code processes still ran"
                f" {STEP_TIMEOUT_S}s later)"
            )

    def _picker_state(self, screen: dict, label: str) -> str:
        if screen["onboarding"] is not None:
            return "onboarding"
        if screen["dialog"] is not None:
            if "Do you trust the authors" in screen["dialog"]:
                return "dialog: trust folder"
            return f"dialog: {screen['dialog'][:200]}"
        if screen["tour"] is not None:
            return "tour"
        if screen["copilotChatOffNotice"] and not screen["dropdown"]:
            return "notice: Copilot Chat off"
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

    def send(self, prompt: str) -> str:
        """Type ``prompt`` into the chat input and send it; returns the prompt to wait on."""
        editor = self.page.locator(".interactive-input-part .interactive-input-editor").first
        self._click("chat input: focus", editor)
        self._act("chat input: type prompt", lambda: self.page.keyboard.type(prompt))
        self.shot("prompt")
        self._act("chat input: Enter", lambda: self.page.keyboard.press("Enter"))
        return prompt

    def wait_for_answer(self, prompt: str) -> str:
        """Wait until the turn for ``prompt`` finishes and return its response text.

        Any tool confirmation or dialog fails the turn."""
        deadline = time.monotonic() + TURN_TIMEOUT_S
        idle_since = None
        prompt = " ".join(prompt.split())
        while time.monotonic() < deadline:
            screen = self.screen()
            if screen["dialog"] is not None:
                raise UnknownScreen(f"Unexpected dialog during the turn: {screen['dialog'][:500]}")
            confirmation = screen["confirmation"]
            if confirmation is not None:
                self.shot("confirmation")
                raise UnknownScreen(f"Unexpected tool confirmation: {confirmation[:500]}")
            response = screen["response"]
            request = screen["request"] or ""
            if prompt in request and response and not response["busy"]:
                # Busy drops only when the whole turn, every tool round included, is over.
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since >= SETTLE_S:
                    self.shot("answer")
                    return response["text"]
            else:
                idle_since = None
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
    return json.loads(package.read_text(encoding="utf-8"))["version"]


def ug_extension_config(session) -> dict:
    """The ug-config.json inside the one installed ug extension."""
    folders = sorted((session.home / ".vscode/extensions").glob("databricks.unity-gateway-*"))
    assert len(folders) == 1, folders
    return json.loads((folders[0] / "ug-config.json").read_text(encoding="utf-8"))


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
