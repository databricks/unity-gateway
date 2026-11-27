"""Exercise the persistent UIA helper against stub and native Windows controls."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

from tests import windows_uia_diagnostics
from tests.integration.utils.windows_app import UIA_CLIENT, WindowsUIAProcess
from tests.windows_uia_diagnostics import capture_native_environment as _capture_native_environment

STUB_TYPES = r"""
using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using Stub;
namespace Stub {
    public class ElementNotAvailableException : Exception { }
    public class AutomationProperty { public string Key; public AutomationProperty(string key) { Key = key; } }
    public class AutomationPattern { public string Key; public AutomationPattern(string key) { Key = key; } }
    public delegate void AutomationPropertyChangedEventHandler(object sender, AutomationPropertyChangedEventArgs args);
    public class AutomationPropertyChangedEventArgs : EventArgs { public object NewValue; }
    public static class Automation {
        public static AutomationPropertyChangedEventHandler Changed;
        public static void AddAutomationPropertyChangedEventHandler(AutomationElement root, TreeScope scope, AutomationPropertyChangedEventHandler handler, params AutomationProperty[] properties) { Changed += handler; }
        public static void RemoveAutomationPropertyChangedEventHandler(AutomationElement root, AutomationPropertyChangedEventHandler handler) { Changed -= handler; }
        public static void Raise(AutomationElement target, string value) { if (Changed != null) Changed(target, new AutomationPropertyChangedEventArgs { NewValue = value }); }
    }
    public enum TreeScope { Children, Descendants, Subtree }
    public abstract class Condition {
        public abstract bool Match(AutomationElement element);
        public static readonly Condition TrueCondition = new All();
    }
    public class All : Condition { public override bool Match(AutomationElement element) { return true; } }
    public class PropertyCondition : Condition {
        private AutomationProperty property; private object value;
        public PropertyCondition(AutomationProperty property, object value) { this.property = property; this.value = value; }
        public override bool Match(AutomationElement element) {
            switch (property.Key) {
                case "type": return Object.Equals(element.Current.ControlType, value);
                case "name": return Object.Equals(element.Current.Name, value);
                case "value": return Object.Equals(State.Value, value);
                case "enabled": return Object.Equals(element.Current.IsEnabled, value);
                case "offscreen": return Object.Equals(element.Current.IsOffscreen, value);
                default: return true;
            }
        }
    }
    public class AndCondition : Condition {
        private Condition[] items;
        public AndCondition(Condition[] items) { this.items = items; }
        public override bool Match(AutomationElement element) { return items.All(item => item.Match(element)); }
    }
    public class OrCondition : Condition {
        private Condition left, right;
        public OrCondition(Condition left, Condition right) { this.left = left; this.right = right; }
        public override bool Match(AutomationElement element) { return left.Match(element) || right.Match(element); }
    }
    public class ControlType {
        public string ProgrammaticName;
        private ControlType(string name) { ProgrammaticName = "ControlType." + name; }
        public static readonly ControlType Button = new ControlType("Button");
        public static readonly ControlType Edit = new ControlType("Edit");
        public static readonly ControlType Text = new ControlType("Text");
        public static readonly ControlType MenuItem = new ControlType("MenuItem");
        public static readonly ControlType Window = new ControlType("Window");
    }
    public class Rect { public int X = 0, Y = 0, Width = 100, Height = 80; }
    public class CurrentInfo {
        public int ProcessId = 123, NativeWindowHandle = 555; public bool IsEnabled = true, IsOffscreen = false;
        public bool HasKeyboardFocus { get { State.Event("focus-check"); return ++State.FocusChecks >= 2; } }
        public string Name; public ControlType ControlType; public Rect BoundingRectangle = new Rect();
    }
    public static class State {
        public static string Value = Environment.GetEnvironmentVariable("UIA_TEST_INITIAL_VALUE") ?? "";
        public static string EditName = Environment.GetEnvironmentVariable("UIA_TEST_EDIT_NAME") ?? "";
        public static int FocusChecks = 0;
        public static bool Opened = false;
        public static void Event(string text) { File.AppendAllText(Environment.GetEnvironmentVariable("UIA_TEST_EVENTS"), text + "\n"); }
    }
    public class AutomationElement {
        public static readonly AutomationProperty ProcessIdProperty = new AutomationProperty("pid");
        public static readonly AutomationProperty ControlTypeProperty = new AutomationProperty("type");
        public static readonly AutomationProperty IsEnabledProperty = new AutomationProperty("enabled");
        public static readonly AutomationProperty IsOffscreenProperty = new AutomationProperty("offscreen");
        public static readonly AutomationProperty NameProperty = new AutomationProperty("name");
        public static readonly AutomationElement RootElement = new AutomationElement(ControlType.Window, "root");
        public static AutomationElement FocusedElement = new AutomationElement(ControlType.Edit, "");
        public CurrentInfo Current;
        public AutomationElement(ControlType type, string name) { Current = new CurrentInfo { ControlType = type, Name = name }; }
        private AutomationElement[] Children() { return new[] { new AutomationElement(ControlType.Button, "Press"), new AutomationElement(ControlType.Edit, State.EditName), new AutomationElement(ControlType.Text, "Ready") }; }
        public AutomationElement[] FindAll(TreeScope scope, Condition condition) {
            if (scope != TreeScope.Children) return Children().Where(condition.Match).ToArray();
            var first = new AutomationElement(ControlType.Window, "window");
            if (!State.Opened) return new[] { first };
            var second = new AutomationElement(ControlType.Window, "popup"); second.Current.NativeWindowHandle = 556;
            return new[] { first, second };
        }
        public AutomationElement FindFirst(TreeScope scope, Condition condition) { return Children().FirstOrDefault(condition.Match); }
        public int[] GetRuntimeId() { return new[] { 123, 456 }; }
        public bool TryGetCurrentPattern(AutomationPattern pattern, out object value) {
            if (pattern.Key == "value" && Current.ControlType == ControlType.Edit) { value = new ValuePattern(); return true; }
            if (pattern.Key == "invoke" && Current.ControlType == ControlType.Button) { value = new InvokePattern(); return true; }
            value = null; return false;
        }
        public void SetFocus() { FocusedElement = this; State.Event("focus"); }
    }
    public class ValuePattern {
        public static readonly AutomationProperty ValueProperty = new AutomationProperty("value");
        public static readonly AutomationPattern Pattern = new AutomationPattern("value");
        public ValueCurrent Current { get { return new ValueCurrent(); } }
    }
    public class ValueCurrent { public string Value { get { return State.Value; } } }
    public class ExpandCollapsePattern { public static readonly AutomationPattern Pattern = new AutomationPattern("expand"); }
    public class InvokePattern { public static readonly AutomationPattern Pattern = new AutomationPattern("invoke"); public void Invoke() { State.Opened = !State.Opened; State.Event("invoke"); } }
    public class SelectionItemPattern { public static readonly AutomationPattern Pattern = new AutomationPattern("selection"); public void Select() { State.Event("select"); } }
    public static class SendKeys { public static void SendWait(string text) { State.Value = ""; foreach (char c in text) { State.Value += c; Automation.Raise(AutomationElement.FocusedElement, State.Value); } State.Event("type"); } }
    public class Bitmap : IDisposable {
        public object Size = new object(); public Bitmap(int width, int height) {} public void Save(string path, object format) { File.WriteAllText(path, "image"); } public void Dispose() {}
    }
    public class Graphics : IDisposable {
        public static Graphics FromImage(Bitmap bitmap) { return new Graphics(); }
        public void CopyFromScreen(int x, int y, int a, int b, object size) {} public void Dispose() {}
    }
    public static class ImageFormat { public static readonly object Png = new object(); }
}
"""


def _stub_uia_script(tmp_path, monkeypatch, *, fail_probe=False):
    pwsh = shutil.which("pwsh")
    assert pwsh, "PowerShell Core is required for the UIA helper contract"
    source = UIA_CLIENT.read_text(encoding="utf-8")
    probe_source = UIA_CLIENT.with_name("windows_uia_probe.cs").read_text(encoding="utf-8")
    probe_source = "\n".join(
        line for line in probe_source.splitlines() if not line.startswith("using ")
    )
    source = source.replace(
        "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes",
        "Add-Type -TypeDefinition @'\n" + STUB_TYPES + "\n" + probe_source + "\n'@",
    )
    source = source.replace(
        "        $probeReferences = @([Windows.Automation.AutomationElement].Assembly.Location, [Windows.Automation.AutomationProperty].Assembly.Location)\n"
        '        Add-Type -Path "$PSScriptRoot/windows_uia_probe.cs" -ReferencedAssemblies $probeReferences',
        "        throw 'probe unavailable'" if fail_probe else "",
    )
    source = source.replace("Add-Type -AssemblyName System.Windows.Forms, System.Drawing", "")
    source = source.replace("[Windows.Automation.", "[Stub.")
    source = source.replace("[Windows.Forms.SendKeys]", "[Stub.SendKeys]")
    source = source.replace("[Drawing.Imaging.ImageFormat]", "[Stub.ImageFormat]")
    source = source.replace("[Drawing.Bitmap]", "[Stub.Bitmap]")
    source = source.replace("[Drawing.Graphics]", "[Stub.Graphics]")
    script = tmp_path / "stub-uia.ps1"
    script.write_text(source, encoding="utf-8")
    events = tmp_path / "events.txt"
    monkeypatch.delenv("UG_UIA_PROBE_ASSEMBLY", raising=False)
    monkeypatch.setenv("UIA_TEST_EVENTS", str(events))
    return pwsh, script, events


def test_powershell_uia_commands_use_one_live_helper_against_stub_controls(
    tmp_path, monkeypatch, record_property
):
    pwsh, script, events = _stub_uia_script(tmp_path, monkeypatch)
    helper = WindowsUIAProcess(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script), "session", "123"]
    )
    try:
        assert "ControlType.Button\tPress" in helper.request("tree")
        assert helper.request("wait", "ControlType.Text", "Ready", "5") == "1"
        for key in (
            "process_to_script_ms",
            "module_ms",
            "uia_assembly_ms",
            "forms_assembly_ms",
            "root_ms",
            "init_ms",
            "process_ready_ms",
            "wait_ms",
        ):
            assert helper.last_metrics[key] >= 0
        assert helper.request("click", "ControlType.Button", "Press", "5", "picker") == "1"
        assert helper.last_diagnostics["activation"] == "invoke"
        assert helper.last_diagnostics["activation_succeeded"] is True
        assert helper.last_diagnostics["target_after"]["expand_collapse_state"] is None
        assert helper.last_metrics["click_ms"] >= 0
        assert helper.last_metrics["wait_ms"] >= 0
        record_property("stub_uia_startup", json.dumps(helper.last_metrics, sort_keys=True))
        with pytest.raises(AssertionError, match="No enabled ControlType.MenuItem"):
            helper.request("click", "ControlType.MenuItem", "Select model", "0")
        assert helper.last_metrics["picker_new_window_count"] == 1
        helper.request("invoke", "ControlType.Button", "Press")
        helper.request("click", "ControlType.Edit", "", "5")
        assert helper.last_diagnostics["target"]["runtime_id"] == [123, 456]
        assert helper.last_diagnostics["target"]["is_enabled"] is True
        assert helper.last_diagnostics["target"]["edit_count"] == 1
        helper.request("type", "abcdefghijklmnop", "")
        assert [sample["value"] for sample in helper.last_diagnostics["composer_samples"]] == [
            "abcdefgh",
            "abcdefghijklmnop",
        ]
        assert all(
            sample["runtime_id"] == [123, 456]
            for sample in helper.last_diagnostics["composer_samples"]
        )
        assert helper.last_diagnostics["composer_final"]["value"] == "abcdefghijklmnop"
        assert helper.last_metrics["composer_focus_before_wait"] == 0
        assert helper.last_metrics["composer_focus_at_first_key"] == 1
        assert helper.last_metrics["composer_focus_wait_ms"] >= 0
        assert helper.last_metrics["click_to_first_key_ms"] >= 0
        assert helper.request("wait", "ControlType.Edit", "abcdefghijklmnop", "5") == "1"
        image = tmp_path / "screenshot.png"
        helper.request("screenshot", str(image))
        assert image.read_text() == "image"
    finally:
        helper.close()
    assert events.read_text().splitlines() == [
        "invoke",
        "invoke",
        "focus",
        "focus-check",
        "focus-check",
        "focus-check",
        "type",
    ]
    legacy = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(script), "tree", "123"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    assert "ControlType.Button\tPress" in legacy.stdout


@pytest.mark.parametrize(
    ("edit_name", "edit_value"),
    [("Alpha\tBeta\nGamma\rDelta", ""), ("", "Alpha\tBeta\nGamma\rDelta")],
)
def test_native_wait_matches_legacy_normalized_name_or_value(
    tmp_path, monkeypatch, edit_name, edit_value
):
    monkeypatch.setenv("UIA_TEST_EDIT_NAME", edit_name)
    monkeypatch.setenv("UIA_TEST_INITIAL_VALUE", edit_value)
    pwsh, script, _ = _stub_uia_script(tmp_path, monkeypatch)
    helper = WindowsUIAProcess(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script), "session", "123"]
    )
    try:
        assert helper.request("wait", "ControlType.Edit", "Alpha Beta Gamma Delta", "0") == "1"
        with pytest.raises(AssertionError, match="No enabled ControlType.Edit"):
            helper.request("wait", "ControlType.Edit", "alpha Beta Gamma Delta", "0")
    finally:
        helper.close()


def test_optional_composer_probe_failure_does_not_block_typing(tmp_path, monkeypatch):
    pwsh, script, _ = _stub_uia_script(tmp_path, monkeypatch, fail_probe=True)
    helper = WindowsUIAProcess(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script), "session", "123"]
    )
    try:
        helper.request("click", "ControlType.Edit", "", "5")
        helper.request("type", "hello", "")
        assert helper.last_diagnostics["composer_probe_error"]
        assert helper.request("wait", "ControlType.Edit", "hello", "5") == "1"
    finally:
        helper.close()


def _wait_for_form_shown(app, ready, timeout):
    deadline = time.monotonic() + timeout
    while not ready.is_file():
        if (code := app.poll()) is not None:
            raise AssertionError(f"Window process exited with {code} before showing its window")
        if time.monotonic() >= deadline:
            raise AssertionError("Test window did not become visible")
        time.sleep(0.05)
    return max(0.0, deadline - time.monotonic())


def _native_failure_details(app, helper, stderr):
    details = {"form_exit": app.poll(), "wait_metrics": dict(helper.last_metrics)}
    try:
        details["tree"] = helper.request("tree")
    except AssertionError as error:
        details["tree_error"] = str(error)
    try:
        details["native_environment"] = _capture_native_environment(app.pid, helper.proc.pid)
    except Exception as error:
        details["native_environment"] = {"error": str(error)}
    if app.poll() is None:
        app.terminate()
        app.wait(timeout=10)
    stderr.seek(0)
    details["form_stderr"] = stderr.read()
    return details


def test_native_form_wait_stops_immediately_if_the_form_process_exits(tmp_path):
    class Form:
        def poll(self):
            return 17

    with pytest.raises(AssertionError, match="exited.*17"):
        _wait_for_form_shown(Form(), tmp_path / "ready", timeout=30)


def test_native_failure_details_preserve_form_exit_stderr_and_wait_metrics(monkeypatch):
    class Form:
        pid = 123

        def poll(self):
            return 17

    class Helper:
        proc = Form()
        last_metrics = {"wait_ms": 30000}

        def request(self, action):
            raise AssertionError("no window")

    monkeypatch.setattr(
        sys.modules[__name__],
        "_capture_native_environment",
        lambda fixture, helper: {"fixture": {"bitness": 64}, "children": []},
        raising=False,
    )
    details = _native_failure_details(Form(), Helper(), io.StringIO("form startup error"))
    assert details == {
        "form_exit": 17,
        "wait_metrics": {"wait_ms": 30000},
        "tree_error": "no window",
        "native_environment": {"fixture": {"bitness": 64}, "children": []},
        "form_stderr": "form startup error",
    }


def test_failure_diagnostics_do_not_mask_the_original_failure(monkeypatch):
    class Form:
        pid = 1

        def poll(self):
            return 1

    class Helper:
        proc = Form()
        last_metrics = {}

        def request(self, _):
            raise AssertionError("no tree")

    def fail_diagnostics(*_):
        raise TypeError("diagnostics failed")

    monkeypatch.setattr(sys.modules[__name__], "_capture_native_environment", fail_diagnostics)
    result = _native_failure_details(Form(), Helper(), io.StringIO("stderr"))
    assert result["tree_error"] == "no tree"
    assert result["native_environment"]["error"] == "diagnostics failed"


def test_native_diagnostics_keep_window_facts_when_uia_inspection_fails(monkeypatch):
    monkeypatch.setattr(
        windows_uia_diagnostics.ctypes, "WinDLL", lambda *a, **k: None, raising=False
    )
    monkeypatch.setattr(windows_uia_diagnostics, "_process_facts", lambda *a: {"bitness": 64})
    monkeypatch.setattr(
        windows_uia_diagnostics,
        "_child_windows",
        lambda *a: [{"handle": 1, "class_name": "Button"}],
    )

    def fail_uia(_):
        raise OSError("UIA unavailable")

    monkeypatch.setattr(windows_uia_diagnostics, "_uia_properties", fail_uia)
    details = windows_uia_diagnostics.capture_native_environment(1, 2)
    assert details["fixture"]["bitness"] == details["helper"]["bitness"] == 64
    assert details["children"] == [{"handle": 1, "class_name": "Button"}]
    assert "UIA unavailable" in details["uia_error"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UI Automation requires native Windows")
def test_powershell_uia_commands_drive_a_real_windows_form(tmp_path, record_property):
    powershell = shutil.which("powershell.exe")
    assert powershell, "Windows PowerShell is required for the UIA helper contract"
    marker = tmp_path / "clicked.txt"
    ready = tmp_path / "shown.txt"
    form = tmp_path / "form.ps1"
    form.write_text(
        textwrap.dedent(
            """\
            Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase
            $form = [Windows.Window]::new()
            $form.Title = 'UG UIA contract'
            $form.Topmost = $true
            $form.Width = 400
            $form.Height = 200
            $form.Add_ContentRendered({ [IO.File]::WriteAllText($env:UIA_TEST_READY, 'shown') })
            $panel = [Windows.Controls.StackPanel]::new()
            $button = [Windows.Controls.Button]::new()
            $button.Content = 'Press'
            $button.Add_Click({ [IO.File]::WriteAllText($env:UIA_TEST_MARKER, 'clicked') })
            $edit = [Windows.Controls.TextBox]::new()
            [void]$panel.Children.Add($button)
            [void]$panel.Children.Add($edit)
            $form.Content = $panel
            [void]$form.ShowDialog()
            """
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "UIA_TEST_MARKER": str(marker), "UIA_TEST_READY": str(ready)}
    form_stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
    try:
        app = subprocess.Popen(
            [powershell, "-NoProfile", "-STA", "-File", str(form)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=form_stderr,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError:
        form_stderr.close()
        raise
    try:
        helper = WindowsUIAProcess(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-File",
                str(UIA_CLIENT),
                "session",
                str(app.pid),
            ]
        )
        try:
            remaining = _wait_for_form_shown(app, ready, timeout=30)
            assert int(helper.request("wait", "ControlType.Button", "Press", f"{remaining:g}")) >= 1
            record_property(
                "windows_powershell_5_1_startup", json.dumps(helper.last_metrics, sort_keys=True)
            )
            assert "ControlType.Button\tPress" in helper.request("tree")
            helper.request("click", "ControlType.Button", "Press", "30")
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert marker.read_text() == "clicked"
            helper.request("invoke", "ControlType.Button", "Press")
            helper.request("click", "ControlType.Edit", "", "30")
            helper.request("type", "hello", "")
            assert int(helper.request("wait", "ControlType.Edit", "hello", "30")) >= 1
            image = tmp_path / "screenshot.png"
            helper.request("screenshot", str(image))
            assert image.stat().st_size > 0
        except Exception as error:
            details = _native_failure_details(app, helper, form_stderr)
            record_property("windows_uia_failure", json.dumps(details, sort_keys=True))
            error.add_note("Native UIA diagnostics: " + json.dumps(details, sort_keys=True))
            raise
        finally:
            helper.close()
        pwsh = shutil.which("pwsh.exe")
        if not pwsh:
            record_property("powershell_7_startup", json.dumps({"status": "not-installed"}))
        else:
            modern = WindowsUIAProcess(
                [
                    pwsh,
                    "-NoProfile",
                    "-NonInteractive",
                    "-STA",
                    "-File",
                    str(UIA_CLIENT),
                    "session",
                    str(app.pid),
                ]
            )
            try:
                try:
                    modern.request("wait", "ControlType.Button", "Press", "30")
                except AssertionError as error:
                    record_property(
                        "powershell_7_startup",
                        json.dumps({"status": "unavailable", "error": str(error)}),
                    )
                else:
                    record_property(
                        "powershell_7_startup", json.dumps(modern.last_metrics, sort_keys=True)
                    )
            finally:
                modern.close()
    finally:
        if app.poll() is None:
            app.terminate()
        app.wait(timeout=10)
        form_stderr.close()
