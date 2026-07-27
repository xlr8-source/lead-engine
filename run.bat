@echo off
cd /d "%~dp0"
echo PayBrix Lead Engine (local)
echo Open http://localhost:8000 in your browser
echo Press Ctrl+C to stop.
echo.
REM Loopback only: this script runs the app on your own machine, where nothing
REM else needs to reach the port. Server and container deployments bind 0.0.0.0
REM instead (see the Dockerfile CMD) and put access control in front of it --
REM host firewall and reverse proxy, plus API_KEY or READ_ONLY_MODE.
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
pause
