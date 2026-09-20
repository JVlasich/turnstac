__version__ = "1.0.0"

import sys
from pathlib import Path

# Vendored deps (libs\ at repo root) win over installed copies, so the shipped
# versions run everywhere. Upgrade a dep = refresh libs\.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "libs"))
