@echo off
REM ===== DataVigil launcher for Windows =====
REM Requires Python 3.10+ on PATH. First run installs dependencies.

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on your PATH.
  echo Please install Python 3.10+ from https://www.python.org/downloads/ and re-run.
  pause
  exit /b 1
)

if not exist ".venv\" (
  echo First-time setup: creating environment and installing DataVigil...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  REM full extras: PostgreSQL/Aurora driver + boto3 (Bedrock, IAM) + mcp + reports
  python -m pip install -e ".[full]"
  REM optional: enables the Aurora Data API (MCP) connect option
  python -m pip install awslabs.postgres-mcp-server 2>nul
) else (
  call .venv\Scripts\activate.bat
)

echo Starting DataVigil...
start "" http://127.0.0.1:8000
python -m datavigil.cli serve --host 127.0.0.1 --port 8000

endlocal
