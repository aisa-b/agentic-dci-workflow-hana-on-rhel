#!/bin/bash
# Run the MCP server using the venv Python.

exec .venv/bin/python -m agents.mcp_server "$@"
