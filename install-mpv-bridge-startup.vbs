Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
startup = sh.SpecialFolders("Startup")
lnk = startup & "\Nexus MPV Bridge.lnk"
Set sc = sh.CreateShortcut(lnk)
sc.TargetPath = dir & "\start-mpv-bridge.vbs"
sc.WorkingDirectory = dir
sc.WindowStyle = 7
sc.Description = "Nexus Player MPV bridge"
sc.Save
MsgBox "MPV bridge will start automatically when Windows boots." & vbCrLf & vbCrLf & "Shortcut: " & lnk, vbInformation, "Nexus MPV Bridge"