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
AppName=TradingAgents Helper
AppVersion={#AppVersion}
DefaultDirName={autopf}\TradingAgents Helper
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=TradingAgentsHelperSetup
Compression=lzma2
SolidCompression=yes

[Files]
Source: "..\..\..\dist\TradingAgentsHelper.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\TradingAgents Helper"; Filename: "{app}\TradingAgentsHelper.exe"

[Run]
Filename: "{app}\TradingAgentsHelper.exe"; Description: "Launch TradingAgents Helper"; Flags: postinstall nowait skipifsilent
