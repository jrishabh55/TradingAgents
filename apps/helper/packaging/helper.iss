; Inno Setup definition for the Windows installer.
; Compiled by build.ps1: iscc /DAppVersion=<version> /Odist helper.iss
;
; Per-user install on purpose (PrivilegesRequired=lowest): no UAC prompt, and
; it matches the helper's per-user state (~/.tradingagents) and its Startup-
; folder autostart registration.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7A1B0C7E-4E1F-4B7A-9C39-2D8E5F0A1B3C}
AppName=Drishti Helper
AppVersion={#AppVersion}
DefaultDirName={autopf}\Drishti Helper
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=DrishtiHelperSetup
Compression=lzma2
SolidCompression=yes

[Files]
; The whole onedir folder — exe + pre-extracted runtime (fast launches).
Source: "..\..\..\dist\DrishtiHelper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Drishti Helper"; Filename: "{app}\DrishtiHelper.exe"

[Run]
Filename: "{app}\DrishtiHelper.exe"; Description: "Launch Drishti Helper"; Flags: postinstall nowait skipifsilent
