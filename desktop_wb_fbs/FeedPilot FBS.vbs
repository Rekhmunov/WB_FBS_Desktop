' Launch FeedPilot FBS without a console window.
Option Explicit
Dim sh, fso, dir, rc, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

' pythonw.exe = GUI process, no black console. WindowStyle 0 = hidden.
On Error Resume Next
rc = sh.Run("cmd /c where pythonw >nul 2>&1", 0, True)
On Error GoTo 0
If rc <> 0 Then
  MsgBox "pythonw не найден в PATH." & vbCrLf & _
         "Установите Python и отметьте Add to PATH," & vbCrLf & _
         "затем в этой папке выполните:" & vbCrLf & _
         "python -m pip install -r requirements.txt" & vbCrLf & vbCrLf & _
         "Для отладки с текстом ошибки используйте ""FeedPilot FBS.bat"".", _
         vbCritical, "FeedPilot FBS"
  WScript.Quit 1
End If

' 0 = hide host window; True = wait for exit code.
rc = sh.Run("pythonw run.py", 0, True)
If rc <> 0 Then
  MsgBox "Программа завершилась с ошибкой (код " & rc & ")." & vbCrLf & _
         "Запустите ""FeedPilot FBS.bat"" — там будет текст ошибки в консоли.", _
         vbExclamation, "FeedPilot FBS"
End If
