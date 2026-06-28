' monitor_guard_launch.vbs — run monitor_guard.ps1 fully hidden (no console
' flash) from Task Scheduler every 5 minutes. Window style 0 = hidden.
CreateObject("Wscript.Shell").Run _
  "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\tvaga\claude_monitor\monitor_guard.ps1""", _
  0, False
