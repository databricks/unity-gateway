"""Explicit workspace configuration shared by full E2E CUJs."""

from typing import ClassVar
from urllib.parse import urlsplit


class BaseCujTest:
    """Declare WORKSPACE_URL on each concrete CUJ class; use self.workspace_url.

    This validates configuration only. Workspace provisioning, exclusive ownership
    across runs, and cleanup must be supplied before implementing live journeys.
    """

    WORKSPACE_URL: ClassVar[str] = ""

    @classmethod
    def validated_workspace_url(cls) -> str:
        value = cls.__dict__.get("WORKSPACE_URL")
        error = f"{cls.__name__} must declare WORKSPACE_URL as an HTTPS workspace origin."
        if not isinstance(value, str) or not value or any(char.isspace() for char in value):
            raise ValueError(error)
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError(error) from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or "?" in value
            or "#" in value
        ):
            raise ValueError(error)
        origin = parsed.netloc.lower()
        if port == 443:
            origin = origin.removesuffix(":443")
        return f"https://{origin}"

    @property
    def workspace_url(self) -> str:
        return self.validated_workspace_url()
