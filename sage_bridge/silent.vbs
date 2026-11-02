' silent.vbs - launch a PowerShell script with no console window at all.
' Scheduled tasks and child launches still flash a console even with
' -WindowStyle Hidden; wscript never creates one.
'   usage: wscript.exe silent.vbs <script.ps1> [args...]
Dim sh, cmd, i
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & WScript.Arguments(0) & """"
For i = 1 To WScript.Arguments.Count - 1
    cmd = cmd & " """ & WScript.Arguments(i) & """"
Next
sh.Run cmd, 0, False
