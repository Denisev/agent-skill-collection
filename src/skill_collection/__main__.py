from __future__ import annotations

import sys

from .cli import main


raise SystemExit(main(sys.argv[1:], stdout=sys.stdout, stderr=sys.stderr))
