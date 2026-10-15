; Inno Setup 6. Double-click installer.cmd for compilation, or open this file
; in the Inno Setup editor and choose Build > Compile (F9).
#if !FileExists("..\..\dist\windows\BossHunter\BossHunter.exe")
  #error Run packaging\windows\build.cmd successfully before compiling this installer.
#endif
#if !FileExists("..\..\dist\windows\version.iss")
  #error The application build is incomplete or its smoke test failed. Re-run build.cmd.
#endif
#include "..\..\dist\windows\version.iss"

[Setup]
AppId={{91C59E64-C1B4-4A92-9658-BB7579CE5A67}
AppName=BossHunter
AppVersion={#MyAppVersion}
AppPublisher=BossHunter Contributors
DefaultDirName={localappdata}\Programs\BossHunter
DefaultGroupName=BossHunter
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\..\dist\installers
OutputBaseFilename=BossHunter-Setup-{#MyAppVersion}-windows-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\..\LICENSE
AppMutex=Local\BossHunter.Desktop
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\BossHunter.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\..\dist\windows\BossHunter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\BossHunter"; Filename: "{app}\BossHunter.exe"
Name: "{autodesktop}\BossHunter"; Filename: "{app}\BossHunter.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\BossHunter.exe"; Description: "Launch BossHunter"; Flags: nowait postinstall skipifsilent

; Deliberately do not remove {localappdata}\BossHunter on uninstall/upgrade:
; it holds the user's configuration, credentials, database and Chrome profile.
