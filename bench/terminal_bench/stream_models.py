"""Find the models Claude reported in an agent log of `--output-format stream-json` lines.

Claude names its model in the `system`/`init` event and on every assistant message. The log
also holds ug's own output, so lines that aren't JSON are skipped. Codex's `--json` events
don't name a model, so a Codex log yields nothing.
"""

import json
from pathlib import Path


def models_in_log(path: Path) -> list[str]:
    """Distinct model ids in first-seen order. Claude's `<synthetic>` placeholders are ignored."""
    models: dict[str, None] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        candidates = [event.get("model") if event.get("type") == "system" else None]
        if event.get("type") == "assistant" and isinstance(message, dict):
            candidates.append(message.get("model"))
        for model in candidates:
            if isinstance(model, str) and model and not model.startswith("<"):
                models.setdefault(model)
    return list(models)
