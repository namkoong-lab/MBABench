#!/usr/bin/env python3
"""
Excel MCP Server
Provides 21 Excel manipulation tools via MCP protocol using FastMCP.

Tools are registered in excel_mcp_server/tools/ via @mcp.tool() decorators.
Shared state (mcp instance, storage path) lives in excel_mcp_server/core/shared_state.py.
"""
import sys
import argparse
from pathlib import Path

# Ensure the project root is on sys.path so `excel_mcp_server` is importable
# when this file is executed directly (e.g., `python excel_mcp_server/server.py`).
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Import shared state (mcp instance, STORAGE_PATH, etc.)
from excel_mcp_server.core import shared_state  # noqa: E402

# Import all tool modules to trigger @mcp.tool() registration
import excel_mcp_server.tools  # noqa: F401, E402

# LibreOffice headless recalculation engine (optional)
try:
    from libreoffice_calc import LibreOfficeCalcEngine
    _LIBREOFFICE_AVAILABLE = True
except ImportError:
    _LIBREOFFICE_AVAILABLE = False
    LibreOfficeCalcEngine = None


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Excel MCP Server")
    parser.add_argument("storage_path", nargs="?", default="./excel_files",
                       help="Path to Excel files storage directory")
    parser.add_argument("--no-libreoffice", action="store_true",
                       help="Disable LibreOffice recalculation engine")
    args = parser.parse_args()

    # Set storage path on shared state
    shared_state.STORAGE_PATH = Path(args.storage_path)
    shared_state.STORAGE_PATH.mkdir(exist_ok=True)

    print(f"Excel MCP Server starting with storage: {shared_state.STORAGE_PATH}", file=sys.stderr)

    # Start LibreOffice engine (if available and not disabled)
    if not args.no_libreoffice and _LIBREOFFICE_AVAILABLE:
        try:
            # Unique UNO port per server process: concurrent batches on one
            # machine each spawn their own MCP server + soffice, and a shared
            # port makes the second server's startup pkill kill the first
            # server's soffice (observed 2026-07-23). UNO_PORT env overrides.
            import os
            uno_port = int(os.environ.get("UNO_PORT", 0)) or (2002 + (os.getpid() % 500))
            shared_state._lo_engine = LibreOfficeCalcEngine(uno_port=uno_port)
            shared_state._lo_engine.start()
            print(f"LibreOffice Calc engine started (UNO port: {shared_state._lo_engine.uno_port})", file=sys.stderr)
        except Exception as e:
            print(f"Warning: LibreOffice engine failed to start: {e}", file=sys.stderr)
            print("Falling back to _eval_formula + xlcalculator", file=sys.stderr)
            shared_state._lo_engine = None
    else:
        if args.no_libreoffice:
            print("LibreOffice engine disabled by --no-libreoffice flag", file=sys.stderr)
        elif not _LIBREOFFICE_AVAILABLE:
            print("LibreOffice engine not available (install libreoffice-calc-nogui)", file=sys.stderr)

    try:
        shared_state.mcp.run()
    finally:
        if shared_state._lo_engine:
            shared_state._lo_engine.stop()


if __name__ == "__main__":
    main()
