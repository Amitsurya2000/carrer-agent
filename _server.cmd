@echo off
REM Internal helper used by start.bat and start_hidden.vbs.
REM Always kills any stale server on port 8000 before launching, so the user
REM never hits "address already in use".
cd /d "C:\Users\Amit\career-agent"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
".\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --app-dir "C:\Users\Amit\career-agent" > server.log 2>&1
