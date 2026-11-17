; Inno Setup script for the Windows installer. Run through scripts/build_app.py, which passes:
;   /DAppVersion=<version>  /DBundle=<the frozen folder>  /DOutDir=<where the installer goes>
;
; Installs for the current user only (no administrator prompt), into %LOCALAPPDATA%\Programs\Rune Advisor,
; with a Start menu entry and an uninstaller. The planner's settings and state live elsewhere
; (%APPDATA%\rune-advisor, %LOCALAPPDATA%\rune-advisor) and are left alone by an uninstall.

[Setup]
AppId={{6B0B7C0E-6F0B-4C55-9B1E-5B7C2C0A1D4F}
AppName=Rune Advisor
AppVersion={#AppVersion}
AppPublisher=Rune Advisor
AppPublisherURL=https://runeadvisor.com
AppSupportURL=https://runeadvisor.com
DefaultDirName={autopf}\Rune Advisor
DefaultGroupName=Rune Advisor
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile={#Bundle}\_internal\LICENSE
OutputDir={#OutDir}
OutputBaseFilename=RuneAdvisor-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Rune Advisor
UninstallDisplayIcon={app}\Rune Advisor.exe

[Files]
Source: "{#Bundle}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Rune Advisor"; Filename: "{app}\Rune Advisor.exe"
Name: "{autodesktop}\Rune Advisor"; Filename: "{app}\Rune Advisor.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\Rune Advisor.exe"; Description: "Open Rune Advisor"; Flags: nowait postinstall skipifsilent
