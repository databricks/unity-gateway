"""Decode Git paths consistently in the added-line scanners."""

import ast


def diff_path(header: str) -> str:
    """Return the path in a ``+++`` header, decoding git's quoting (``"b/donn\\303\\251es.py"``)."""
    if header.startswith('"'):
        try:
            header = ast.literal_eval("b" + header).decode("utf-8", "replace")
        except (SyntaxError, ValueError):
            pass
    return header.removeprefix("b/")
