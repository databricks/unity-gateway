"""TLS trust-store selection: ``UG_TLS_TRUST_STORE``, the saved ``tls_trust_store``, the default."""

from __future__ import annotations

import json
import sys
import types

import pytest
import typer

import ucode.cli as cli_mod
import ucode.config_io as config_io_mod
import ucode.state as state_mod
from ucode import tls


@pytest.fixture
def fake_truststore(monkeypatch):
    """Record inject/extract calls instead of patching the real ``ssl`` module."""
    calls: list[str] = []
    module = types.ModuleType("truststore")
    module.inject_into_ssl = lambda: calls.append("inject")  # type: ignore[attr-defined]
    module.extract_from_ssl = lambda: calls.append("extract")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "truststore", module)
    return calls


def _write_state(**extra) -> None:
    state_mod.STATE_PATH.write_text(
        json.dumps(
            {
                "state_version": state_mod.STATE_VERSION,
                "current_workspace": None,
                "workspaces": {},
                **extra,
            }
        ),
        encoding="utf-8",
    )


class TestResolveTrustStore:
    def test_defaults_to_os(self):
        assert tls.DEFAULT_TRUST_STORE == "os"
        assert tls.resolve_trust_store() == "os"

    @pytest.mark.parametrize("saved", ["os", "python"])
    def test_saved_choice_is_used(self, saved):
        _write_state(tls_trust_store=saved)
        assert tls.resolve_trust_store() == saved
        assert tls.saved_trust_store() == saved

    @pytest.mark.parametrize(("env", "saved"), [("os", "python"), ("python", "os")])
    def test_env_var_overrides_saved_choice(self, monkeypatch, env, saved):
        _write_state(tls_trust_store=saved)
        monkeypatch.setenv(tls.TRUST_STORE_ENV_VAR, env)
        assert tls.resolve_trust_store() == env

    def test_env_var_is_trimmed_and_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(tls.TRUST_STORE_ENV_VAR, "  OS ")
        assert tls.resolve_trust_store() == "os"

    def test_empty_env_var_falls_through_silently(self, monkeypatch, capsys):
        _write_state(tls_trust_store="python")
        monkeypatch.setenv(tls.TRUST_STORE_ENV_VAR, " ")
        assert tls.resolve_trust_store() == "python"
        assert capsys.readouterr().err == ""

    def test_unknown_env_var_warns_and_falls_through(self, monkeypatch, capsys):
        _write_state(tls_trust_store="python")
        monkeypatch.setenv(tls.TRUST_STORE_ENV_VAR, "system")
        assert tls.resolve_trust_store() == "python"
        err = capsys.readouterr().err
        assert "UG_TLS_TRUST_STORE='system'" in err
        assert "python, os" in err

    def test_unknown_saved_choice_warns_and_uses_default(self, capsys):
        _write_state(tls_trust_store="keychain")
        assert tls.resolve_trust_store() == "os"
        assert tls.saved_trust_store() is None
        assert "tls_trust_store='keychain'" in capsys.readouterr().err

    def test_unreadable_state_uses_default(self):
        state_mod.STATE_PATH.write_text("{not json", encoding="utf-8")
        assert tls.resolve_trust_store() == "os"


class TestApply:
    def test_python_leaves_ssl_alone(self, monkeypatch, fake_truststore):
        monkeypatch.setenv(tls.TRUST_STORE_ENV_VAR, "python")
        assert tls.apply() == "python"
        assert fake_truststore == []

    def test_os_injects_once(self, fake_truststore):
        assert tls.apply() == "os"
        assert tls.apply() == "os"
        assert fake_truststore == ["inject"]

    def test_switching_back_to_python_extracts(self, fake_truststore):
        tls.apply("os")
        assert tls.apply("python") == "python"
        assert fake_truststore == ["inject", "extract"]

    def test_inject_failure_warns_and_keeps_python(self, monkeypatch, fake_truststore, capsys):
        def fail() -> None:
            raise OSError("no store")

        monkeypatch.setattr(sys.modules["truststore"], "inject_into_ssl", fail)
        assert tls.apply("os") == "python"
        assert "could not use the OS certificate store (no store)" in capsys.readouterr().err

    def test_os_store_reaches_new_ssl_contexts(self):
        """The real dependency: after apply, clients that build a default context get truststore's."""
        import ssl

        import truststore

        try:
            assert tls.apply("os") == "os"
            assert isinstance(ssl.create_default_context(), truststore.SSLContext)
        finally:
            tls.apply("python")
        assert not isinstance(ssl.create_default_context(), truststore.SSLContext)


class TestSavedTrustStore:
    def test_save_keeps_other_state(self):
        _write_state(config_scope="user")
        tls.save_trust_store("os")
        saved = json.loads(state_mod.STATE_PATH.read_text(encoding="utf-8"))
        assert saved["tls_trust_store"] == "os"
        assert saved["config_scope"] == "user"

    def test_save_is_skipped_under_dry_run(self, monkeypatch):
        monkeypatch.setattr(config_io_mod, "_dry_run", True)
        tls.save_trust_store("os")
        assert not state_mod.STATE_PATH.exists()

    def test_save_rejects_unknown_value(self):
        with pytest.raises(ValueError, match="Unknown TLS trust store"):
            tls.save_trust_store("system")

    def test_revert_clears_saved_choice(self):
        tls.save_trust_store("os")
        state_mod.clear_state()
        assert tls.saved_trust_store() is None


class TestConfigureFlag:
    def test_saves_and_applies_before_any_request(self, fake_truststore):
        tls.apply()
        cli_mod._configure_tls_trust_store(" Python ")
        assert tls.saved_trust_store() == "python"
        assert fake_truststore == ["inject", "extract"]
        assert tls.effective_trust_store() == "python"

    def test_unknown_value_exits_without_saving(self, fake_truststore, capsys):
        with pytest.raises(typer.Exit) as exc:
            cli_mod._configure_tls_trust_store("system")
        assert exc.value.exit_code == 2
        assert tls.saved_trust_store() is None
        assert fake_truststore == []

    def test_env_var_still_wins_and_is_reported(self, monkeypatch, fake_truststore):
        warnings: list[str] = []
        monkeypatch.setattr(cli_mod, "print_warning", warnings.append)
        monkeypatch.setenv(tls.TRUST_STORE_ENV_VAR, "python")
        cli_mod._configure_tls_trust_store("os")
        assert tls.saved_trust_store() == "os"
        assert fake_truststore == []
        assert "UG_TLS_TRUST_STORE=python overrides" in warnings[0]


class TestCertificateFailureHint:
    def test_os_store_hint_names_the_store_and_the_escape_hatch(self, fake_truststore):
        tls.apply("os")
        hint = tls.certificate_failure_hint()
        assert "operating system's trust store" in hint
        assert "UG_TLS_TRUST_STORE=python" in hint
        assert "SSL_CERT_FILE and REQUESTS_CA_BUNDLE" in hint

    def test_python_chain_hint_points_at_the_os_store(self, fake_truststore):
        tls.apply("python")
        hint = tls.certificate_failure_hint()
        assert "UG_TLS_TRUST_STORE=os" in hint
        assert "SSL_CERT_FILE and REQUESTS_CA_BUNDLE" in hint

    def test_only_verification_failures_get_the_hint(self):
        import ssl

        from ucode.databricks import _network_error_reason

        failed = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] self-signed")
        assert tls.is_certificate_failure(failed)
        assert _network_error_reason(failed).startswith(
            "network error: certificate verification failed: [SSL: CERTIFICATE_VERIFY_FAILED]"
        )
        bare = ssl.SSLCertVerificationError("certificate is not trusted")
        assert _network_error_reason(bare).startswith(
            "network error: certificate verification failed: certificate is not trusted. ug "
        )
        assert "UG_TLS_TRUST_STORE" in _network_error_reason(failed)
        assert _network_error_reason("Connection refused") == "network error: Connection refused"


class TestUvEnvironment:
    def test_os_store_makes_uv_read_system_certificates(self, fake_truststore):
        tls.apply("os")
        env = tls.uv_environment({"PATH": "/bin"})
        assert env == {"PATH": "/bin", "UV_SYSTEM_CERTS": "1", "UV_NATIVE_TLS": "1"}

    def test_user_values_are_kept(self, fake_truststore):
        tls.apply("os")
        env = tls.uv_environment({"UV_SYSTEM_CERTS": "0"})
        assert env["UV_SYSTEM_CERTS"] == "0"

    def test_python_chain_leaves_uv_alone(self, fake_truststore):
        tls.apply("python")
        assert tls.uv_environment({"PATH": "/bin"}) == {"PATH": "/bin"}


class TestProcessEntryPoints:
    """Every process that makes requests applies the trust store before its work."""

    def test_console_script_applies_before_the_command(self, monkeypatch):
        import ucode.entrypoint as entrypoint_mod

        order: list[str] = []
        monkeypatch.setattr(sys, "argv", ["ug", "status"])
        monkeypatch.setattr(tls, "apply", lambda *_: order.append("tls") or "python")
        monkeypatch.setattr(cli_mod, "app", lambda: order.append("command"))
        entrypoint_mod.main()
        assert order[0] == "tls"
        assert order[-1] == "command"

    def test_module_launch_applies(self, monkeypatch):
        order: list[str] = []
        monkeypatch.setattr(tls, "apply", lambda: order.append("tls") or "python")
        monkeypatch.setattr(cli_mod, "app", lambda: order.append("command"))
        cli_mod.main()
        assert order == ["tls", "command"]

    def test_mcp_proxy_backend_inherits_the_env_var(self):
        from ucode.mcp_proxy_runtime import _RUNTIME_ENV_KEYS

        assert tls.TRUST_STORE_ENV_VAR in _RUNTIME_ENV_KEYS
