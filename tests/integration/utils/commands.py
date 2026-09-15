"""Shared command construction for the live integration suite."""

CODEX_TEST_MODEL = "system.ai.gpt-5-4-nano"


def pin_codex_test_model(command: list[str]) -> list[str]:
    """Pin Codex requests away from the heavily rate-limited Astra default."""
    if len(command) < 2 or command[1] != "codex":
        return command
    forwarded = command[2:]
    if any(
        arg == "-m" or arg == "--model" or arg.startswith("--model=") for arg in forwarded
    ):
        return command
    if "--" not in forwarded:
        return [*command[:2], "--", "--model", CODEX_TEST_MODEL, *forwarded]
    separator = command.index("--")
    return [
        *command[: separator + 1],
        "--model",
        CODEX_TEST_MODEL,
        *command[separator + 1 :],
    ]
