' Launch FeedPilot FBS without a console window.
Option Explicit
Dim sh, fso, dir, python, cmd, rc
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

python = "python"
On Error Resume Next
rc = sh.Run("cmd /c where python >nul 2>&1", 0, True)
On Error GoTo 0
If rc <> 0 Then
  MsgBox "Python не найден в PATH." & vbCrLf & _
         "Установите Python и отметьте Add to PATH," & vbCrLf & _
         "затем в этой папке выполните:" & vbCrLf & _
         "python -m pip install -r requirements.txt", _
         vbCritical, "FeedPilot FBS"
  WScript.Quit 1
End If

rc = sh.Run("python run.py", 1, True)
If rc <> 0 Then
  MsgBox "Программа завершилась с ошибкой (код " & rc & ")." & vbCrLf & _
         "Попробуйте запустить ""FeedPilot FBS.bat"" — там будет текст ошибки.", _
         vbExclamation, "FeedPilot FBS"
End If
