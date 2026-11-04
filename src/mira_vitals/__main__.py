"""``python -m mira_vitals``."""

import sys

from .cli import main

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "ci":
        from .ci.command import main as ci_main

        sys.exit(ci_main(sys.argv[2:]))
    sys.exit(main())
