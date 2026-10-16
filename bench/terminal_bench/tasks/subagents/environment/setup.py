"""Write random data files to audit."""

import os
import sys
from pathlib import Path

data = Path(sys.argv[1], "data")
data.mkdir(parents=True, exist_ok=True)
for name in ["alpha.bin", "bravo.bin", "charlie.bin"]:
    (data / name).write_bytes(os.urandom(4096))
