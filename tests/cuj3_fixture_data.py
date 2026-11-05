"""Shared model-service inputs for CUJ3 component tests."""


def model_readback(*, server_defaults):
    destination = {
        "name": "system.ai.databricks-gpt-6-luna",
        "destination_type": "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL",
        "pay_per_token_config": {"model": "models/system.ai.databricks-gpt-6-luna"},
    }
    routing = {"destinations": [destination]}
    if server_defaults:
        destination.update(traffic_percentage=100, is_deleted=False)
        routing["fallback"] = {"destinations": []}
    return {
        "name": "model-services/ug_e2e.models.gpt_luna",
        "config": {"routing": routing, "usage_tracking": {"enabled": True}},
    }


INVALID_DESTINATIONS = [
    {"name": "system.ai.wrong_source"},
    {"name": "other.ai.fixture"},
    {"destination_type": "DESTINATION_TYPE_EXTERNAL_MODEL"},
    {"pay_per_token_config": {"model": "models/system.ai.wrong_source"}},
    {"pay_per_token_config": None},
    {"traffic_percentage": 0},
    {"traffic_percentage": 50},
    {"traffic_percentage": "100"},
    {"traffic_percentage": None},
    {"traffic_percentage": True},
    {"is_deleted": True},
    {"is_deleted": "false"},
    {"is_disabled": True},
    {"disabled": True},
]
