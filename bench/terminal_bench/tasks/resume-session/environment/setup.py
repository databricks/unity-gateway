"""Write the vault file the first step reads."""

import sys
from pathlib import Path

Path(sys.argv[1]).mkdir(parents=True, exist_ok=True)
Path(sys.argv[1], "vault.txt").write_text("passphrase: amber-falcon-7342\n")
