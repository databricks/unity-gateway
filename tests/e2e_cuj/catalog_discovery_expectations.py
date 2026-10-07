"""Model fixtures owned by the catalog discovery journey."""

MODEL_SCHEMA = "ug_e2e.models"
OTHER_MODEL_SCHEMA = "ug_e2e.other_models"
CLAUDE_DEFAULT = f"{MODEL_SCHEMA}.claude_sonnet"
CODEX_DEFAULT = f"{MODEL_SCHEMA}.gpt_luna"
GEMINI_MODEL = f"{MODEL_SCHEMA}.gemini_flash"
CLAUDE_MODELS = frozenset({CLAUDE_DEFAULT, f"{MODEL_SCHEMA}.claude_haiku", f"{MODEL_SCHEMA}.kimi"})
CODEX_MODELS = frozenset({CODEX_DEFAULT, f"{MODEL_SCHEMA}.kimi"})
CLAUDE_DECOY = f"{OTHER_MODEL_SCHEMA}.claude_decoy"
CODEX_DECOY = f"{OTHER_MODEL_SCHEMA}.codex_decoy"
