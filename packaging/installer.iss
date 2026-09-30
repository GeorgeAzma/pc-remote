; PC Remote installer (Inno Setup 6). Built by packaging/build.py, which
; passes AppVersion, SourceDir (the PyInstaller folder) and OutputDir.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7C3F1E25-B4D4-4E8A-9C21-3F6B8D0E4A17}
AppName=PC Remote
AppVersion={#AppVersion}
AppVerName=PC Remote {#AppVersion}
AppPublisher=GeorgeAzma
AppPublisherURL=https://github.com/GeorgeAzma/pc-remote
AppSupportURL=https://github.com/GeorgeAzma/pc-remote/issues
DefaultDirName={autopf}\PC Remote
DisableProgramGroupPage=yes
DisableDirPage=auto
; admin: the firewall rules and the startup task (which runs PC Remote with
; admin rights, so it can type into admin windows)
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=PC-Remote-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\PC Remote.exe
UninstallDisplayName=PC Remote
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UsedUserAreasWarning=no
#ifdef Sign
SignTool=signtool
SignedUninstaller=yes
#endif

[Tasks]
Name: startup; Description: "Start PC Remote when you sign in (recommended)"
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\PC Remote"; Filename: "{app}\PC Remote.exe"; Comment: "Control this PC from your phone"
Name: "{autodesktop}\PC Remote"; Filename: "{app}\PC Remote.exe"; Tasks: desktopicon

[Run]
; firewall rules (home networks + Tailscale), and the sign-in task if chosen
Filename: "{app}\PC Remote.exe"; Parameters: "--setup"; Tasks: startup; Flags: runhidden waituntilterminated; StatusMsg: "Setting up start at sign-in and the firewall…"
Filename: "{app}\PC Remote.exe"; Parameters: "--setup --no-startup"; Tasks: not startup; Flags: runhidden waituntilterminated; StatusMsg: "Setting up the firewall…"
; silent installs: start it right away
Filename: "{sys}\schtasks.exe"; Parameters: "/Run /TN ""PC Remote"""; Tasks: startup; Flags: runhidden; Check: WizardSilent
; the last page: open it (as you, not as the admin that installed it)
Filename: "{app}\PC Remote.exe"; Description: "Start PC Remote and show its addresses"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM ""PC Remote.exe"""; Flags: runhidden; RunOnceId: "Stop"
Filename: "{app}\PC Remote.exe"; Parameters: "--unsetup"; Flags: runhidden waituntilterminated; RunOnceId: "Unsetup"

[UninstallDelete]
; its certificates and log
Type: filesandordirs; Name: "{localappdata}\PC Remote"

[Code]
// An update replaces files the running copy has open: stop it first.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "PC Remote.exe"', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
