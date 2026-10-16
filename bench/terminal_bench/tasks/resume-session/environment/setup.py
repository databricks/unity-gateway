"""Write the note the first step reads."""

import sys
from pathlib import Path

Path(sys.argv[1]).mkdir(parents=True, exist_ok=True)
Path(sys.argv[1], "note.txt").write_text("code word: amber-falcon-7342\n")
