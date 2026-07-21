@echo off
cd /d "%~dp0"
echo PayBrix Lead Engine
echo Open http://localhost:8000 in your browser
echo Press Ctrl+C to stop.
echo.
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000
pause
