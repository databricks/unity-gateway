import os
import re


def redact(text):
    for key in ("DATABRICKS_BEARER", "CLAUDE_CODE_OAUTH_TOKEN", "UG_COWORK_PAT"):
        secret = os.environ.get(key)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\bsk-ant-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"\bdapi[0-9a-f]{32}(-\d+)?\b", "[REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", text)
    return re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", text)
