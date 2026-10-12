"""`python -m kg.mcp`: run the knowledge graph's read-only MCP server (kg/mcp_server.py)."""

import sys

from kg.mcp_server import main

if __name__ == "__main__":
    sys.exit(main())
