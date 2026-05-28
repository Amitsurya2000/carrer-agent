@echo off
title AI Career Agent - leave this window OPEN
cd /d "C:\Users\Amit\career-agent"
echo ============================================================
echo   AI Career Agent
echo.
echo   Stopping any old server on port 8000 first...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
echo   Starting fresh server.
echo.
echo   Keep THIS window OPEN while you use the app.
echo   Browser:  http://127.0.0.1:8000
echo   To stop:  close this window.
echo ============================================================
echo.
".\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir "C:\Users\Amit\career-agent\app" --app-dir "C:\Users\Amit\career-agent"
echo.
echo Server stopped. Press any key to close.
pause >nul
