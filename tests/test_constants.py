import pytest

from ucode.constants import MODEL_DISCOVERY_ENV_VAR, scoped_model_discovery_enabled


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        ("", True),
        ("1", True),
        ("false", True),
        ("0", False),
    ],
)
def test_scoped_model_discovery_environment_policy(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(MODEL_DISCOVERY_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(MODEL_DISCOVERY_ENV_VAR, value)

    assert scoped_model_discovery_enabled() is expected


@pytest.mark.parametrize(
    ("environment", "override", "expected"),
    [("0", True, True), ("1", False, False)],
)
def test_scoped_model_discovery_override_wins(monkeypatch, environment, override, expected):
    monkeypatch.setenv(MODEL_DISCOVERY_ENV_VAR, environment)

    assert scoped_model_discovery_enabled(override=override) is expected


def test_scoped_model_discovery_force_wins(monkeypatch):
    monkeypatch.setenv(MODEL_DISCOVERY_ENV_VAR, "0")

    assert scoped_model_discovery_enabled(override=False, force=True) is True
