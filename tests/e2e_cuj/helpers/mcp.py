"""Names under which ug registers managed MCP services with each agent."""


def registered_name(service: str) -> str:
    """The server name ug registers for a UC MCP service, for example `ug_e2e-tools-x`."""
    return service.replace(".", "-")
