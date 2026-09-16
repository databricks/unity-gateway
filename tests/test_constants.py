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


def test_scoped_model_discovery_managed_config_wins(monkeypatch):
    monkeypatch.setenv(MODEL_DISCOVERY_ENV_VAR, "0")

    assert scoped_model_discovery_enabled(managed_config_exists=True) is True
