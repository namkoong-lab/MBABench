"""
LibreOffice Headless Integration for Excel MCP Server

Manages a persistent LibreOffice process and provides formula recalculation
using LibreOffice Calc's engine, which supports ALL Excel functions including
PMT, IRR, VLOOKUP, INDEX/MATCH, etc.

Uses UNO socket bridge: a persistent soffice process is started once, and
a helper script (uno_recalc_helper.py) connects via UNO to recalculate files.
"""

import subprocess
import time
import os
import logging
import tempfile
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Path to the UNO helper script (runs under system Python)
_UNO_HELPER_SCRIPT = Path(__file__).parent / "uno_recalc_helper.py"

# Python interpreter with UNO bindings. Resolution order:
#   1. UNO_PYTHON env var (explicit override)
#   2. macOS: LibreOffice.app's bundled interpreter (the only UNO-capable
#      python on macOS; note its parent launch constraint must be stripped
#      via ad-hoc codesign before it can be spawned from outside the app)
#   3. /usr/bin/python3 (Linux default — python3-uno apt package)
import os as _os
import sys as _sys

# The Resources/python wrapper sets PYTHONPATH/URE bootstrap vars before
# exec'ing the framework interpreter — required for `import uno` to resolve.
_MACOS_LO_PYTHON = "/Applications/LibreOffice.app/Contents/Resources/python"
_SYSTEM_PYTHON = _os.environ.get("UNO_PYTHON") or (
    _MACOS_LO_PYTHON
    if _sys.platform == "darwin" and _os.path.exists(_MACOS_LO_PYTHON)
    else "/usr/bin/python3"
)


class LibreOfficeCalcEngine:
    """Manages a persistent LibreOffice headless process for formula recalculation."""

    def __init__(self, uno_port: int = 2002, recalc_timeout: float = 30.0):
        self.uno_port = uno_port
        self.recalc_timeout = recalc_timeout
        self._soffice_process: Optional[subprocess.Popen] = None
        self._profile_dir: Optional[str] = None
        self._started = False
        self._lock = threading.Lock()

    def start(self):
        """Start the LibreOffice engine. Call once at MCP server startup."""
        self._start_soffice_listener()
        self._started = True
        logger.info(f"LibreOffice UNO listener started on port {self.uno_port}")

    def stop(self):
        """Stop the LibreOffice engine. Call at MCP server shutdown."""
        if self._soffice_process:
            try:
                self._soffice_process.terminate()
                self._soffice_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._soffice_process.kill()
                self._soffice_process.wait(timeout=5)
            except Exception:
                try:
                    self._soffice_process.kill()
                except Exception:
                    pass
            finally:
                self._soffice_process = None
        self._started = False
        logger.info("LibreOffice engine stopped")

    def recalculate(self, file_path: str) -> dict:
        """
        Recalculate all formulas in the given .xlsx file.

        The file is modified in-place with cached formula values written back.
        After this call, openpyxl.load_workbook(data_only=True) will return
        computed values for all formulas.

        Returns:
            dict with keys: success (bool), duration_ms (float), error (str|None)
        """
        if not self._started:
            return {"success": False, "duration_ms": 0, "error": "LibreOffice engine not started"}

        with self._lock:
            # Auto-restart if soffice died
            if self._soffice_process and self._soffice_process.poll() is not None:
                logger.warning("soffice process died, attempting restart...")
                try:
                    self._start_soffice_listener()
                except Exception as e:
                    return {"success": False, "duration_ms": 0, "error": f"soffice restart failed: {e}"}

            start_time = time.monotonic()
            try:
                result = subprocess.run(
                    [
                        _SYSTEM_PYTHON,
                        str(_UNO_HELPER_SCRIPT),
                        "--recalc",
                        str(self.uno_port),
                        file_path,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.recalc_timeout,
                )
                duration = (time.monotonic() - start_time) * 1000

                if result.returncode == 0:
                    return {"success": True, "duration_ms": round(duration, 1), "error": None}
                else:
                    error_msg = result.stderr.strip() or result.stdout.strip()
                    return {"success": False, "duration_ms": round(duration, 1), "error": error_msg}

            except subprocess.TimeoutExpired:
                duration = (time.monotonic() - start_time) * 1000
                return {"success": False, "duration_ms": round(duration, 1), "error": f"Recalculation timed out after {self.recalc_timeout}s"}
            except Exception as e:
                duration = (time.monotonic() - start_time) * 1000
                return {"success": False, "duration_ms": round(duration, 1), "error": str(e)}

    @property
    def is_running(self) -> bool:
        return self._started and self._soffice_process is not None and self._soffice_process.poll() is None

    def _start_soffice_listener(self):
        """Start soffice with UNO socket listener."""
        # Kill any existing soffice processes on our port
        subprocess.run(["pkill", "-f", f"accept=socket.*port={self.uno_port}"], capture_output=True)
        time.sleep(1)

        # Create a unique user profile to avoid lock conflicts
        self._profile_dir = tempfile.mkdtemp(prefix="lo_profile_")

        self._soffice_process = subprocess.Popen(
            [
                "soffice",
                "--headless",
                "--norestore",
                "--nologo",
                "--nodefault",
                f"-env:UserInstallation=file://{self._profile_dir}",
                f"--accept=socket,host=localhost,port={self.uno_port};urp;",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # Wait for soffice to be ready (poll UNO connection)
        for attempt in range(20):  # Up to 10 seconds
            time.sleep(0.5)
            if self._soffice_process.poll() is not None:
                stderr = self._soffice_process.stderr.read().decode()
                raise RuntimeError(f"soffice exited immediately: {stderr}")
            # Try a test connection
            try:
                test_result = subprocess.run(
                    [_SYSTEM_PYTHON, str(_UNO_HELPER_SCRIPT), "--ping", str(self.uno_port)],
                    capture_output=True, text=True, timeout=5
                )
                if test_result.returncode == 0:
                    return
            except subprocess.TimeoutExpired:
                continue

        raise RuntimeError("soffice started but UNO connection not ready after 10s")
