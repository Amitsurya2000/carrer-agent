' Launches the AI Career Agent server hidden, after first killing any stale server
' on port 8000. A copy of this file lives in the Windows Startup folder, so the
' server starts automatically every time you log in.
Set sh = CreateObject("WScript.Shell")
sh.Run """C:\Users\Amit\career-agent\_server.cmd""", 0, False
