"""`python -m m365`: run the whispr-m365 MCP server (m365/server.py)."""

import sys

from m365.server import main

if __name__ == "__main__":
    sys.exit(main())
