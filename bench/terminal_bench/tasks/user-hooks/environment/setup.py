"""Store the codename the hook hands to the agent."""

import sys
from pathlib import Path

Path(sys.argv[1], ".claude", "hooks", "codename").write_text("velvet-harbor-58\n")
