@echo off
title Search Jobs - Visible Chromium
cd /d "C:\Users\Amit\career-agent"
echo ============================================================
echo   Search Jobs (Visible Chromium)
echo.
echo   A Chromium window will pop up in a moment. You will see
echo   it open Hacker News, find the latest hiring thread, and
echo   extract every job post.
echo.
echo   The window will close itself when done.
echo ============================================================
echo.
".\.venv\Scripts\python.exe" -m app.scrape.hn_hiring
echo.
echo Done. Press any key to close.
pause >nul
