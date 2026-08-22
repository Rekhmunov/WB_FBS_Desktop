' Launch FeedPilot FBS without a console window.
Option Explicit
Dim sh, fso, dir, rc, cmd, crashPath, crashText, msg, stm
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

On Error Resume Next
rc = sh.Run("cmd /c where pythonw >nul 2>&1", 0, True)
On Error GoTo 0
If rc <> 0 Then
  MsgBox "pythonw not found in PATH." & vbCrLf & vbCrLf & _
         "Install Python (Add to PATH), then run:" & vbCrLf & _
         "python -m pip install -r requirements.txt" & vbCrLf & vbCrLf & _
         "For error text use FeedPilot FBS.bat", vbCritical, "FeedPilot FBS"
  WScript.Quit 1
End If

rc = sh.Run("pythonw run.py", 0, True)
If rc <> 0 Then
  crashPath = sh.ExpandEnvironmentStrings("%APPDATA%") & "\FeedPilotFBS\logs\last_crash.txt"
  crashText = ""
  If fso.FileExists(crashPath) Then
    On Error Resume Next
    Set stm = CreateObject("ADODB.Stream")
    stm.Type = 2
    stm.Charset = "utf-8"
    stm.Open
    stm.LoadFromFile crashPath
    crashText = Left(stm.ReadText, 900)
    stm.Close
    On Error GoTo 0
  End If
  msg = "FeedPilot FBS failed to start (exit code " & rc & ")." & vbCrLf & vbCrLf
  If Len(crashText) > 0 Then
    msg = msg & crashText & vbCrLf & vbCrLf
  Else
    msg = msg & "Run FeedPilot FBS.bat in cmd for error details." & vbCrLf & vbCrLf & _
          "Usually: python -m pip install -r requirements.txt" & vbCrLf & vbCrLf
  End If
  msg = msg & "Log: " & crashPath
  MsgBox msg, vbExclamation, "FeedPilot FBS"
End If
