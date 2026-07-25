; Inno Setup 6 script for OpenKimo (Windows).
;
; Compiled via build_windows.py with preprocessor variables:
;   /DAppName=...        e.g. "OpenKimo"
;   /DAppVersion=...     e.g. "0.1.4"
;   /DStagingDir=...     absolute path to build/runtime-staging
;   /DIconFile=...       absolute path to OpenKimo.ico (used as SetupIconFile)
;   /DOutputDir=...      absolute path where the .exe installer is written
;
; Defaults below let the script compile by hand for sanity-checking
; (iscc.exe installer.iss) without args — it will fail at [Files] because
; StagingDir doesn't exist, but the preprocessor validates first.

#ifndef AppId
  ; Double braces escape the literal "{" — Inno Setup would otherwise treat
  ; it as the start of a constant. White-label builds may override via /DAppId=...
  #define AppId "{{38AF4995-832E-45E5-A517-81A770BF3A69}}"
#endif
#ifndef AppName
  #define AppName "OpenKimo"
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StagingDir
  #define StagingDir "build\runtime-staging"
#endif
#ifndef IconFile
  #define IconFile "icon.ico"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist-win"
#endif

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=OpenKimo Contributors
AppPublisherURL=https://github.com/j0x7c4/OpenKimo
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename={#AppName}Setup-{#AppVersion}
SetupIconFile={#IconFile}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\OpenKimo.exe
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "purgeuserdata"; Description: "Also remove user data (%AppData%\{#AppName}) on uninstall"; GroupDescription: "Uninstall behaviour:"; Flags: unchecked

[Files]
Source: "{#StagingDir}\runtime\*"; DestDir: "{app}\runtime"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#StagingDir}\OpenKimo.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StagingDir}\brand.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StagingDir}\TrayIcon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\OpenKimo.exe"; IconFilename: "{app}\TrayIcon.ico"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\OpenKimo.exe"; IconFilename: "{app}\TrayIcon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\OpenKimo.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Always nuke the install dir contents (runtime/, exe, etc).
Type: filesandordirs; Name: "{app}\runtime"
Type: files; Name: "{app}\purge-user-data.marker"

[Code]
var
  PurgeUserData: Boolean;

function PurgeMarkerPath(): String;
begin
  Result := ExpandConstant('{app}\purge-user-data.marker');
end;

// WizardIsTaskSelected only exists while the install wizard is alive, so the
// task choice is persisted to a marker file here and read back at uninstall
// (calling it from uninstall code raises "cannot call WizardIsTaskSelected").
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('purgeuserdata') then
      SaveStringToFile(PurgeMarkerPath(), '1', False)
    else
      DeleteFile(PurgeMarkerPath());
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  UserDataDir: String;
begin
  // Read the marker before the uninstaller removes {app} contents.
  if CurUninstallStep = usUninstall then
    PurgeUserData := FileExists(PurgeMarkerPath());
  if CurUninstallStep = usPostUninstall then
  begin
    if PurgeUserData then
    begin
      UserDataDir := ExpandConstant('{userappdata}\{#AppName}');
      if DirExists(UserDataDir) then
      begin
        DelTree(UserDataDir, True, True, True);
      end;
    end;
  end;
end;
