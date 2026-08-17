"""Allow ``python -m jaigent``."""

import sys

from jaigent.cli import main

if __name__ == "__main__":
    sys.exit(main())
