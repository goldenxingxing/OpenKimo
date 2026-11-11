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
; The Restart Manager lists a running OpenKimo as three "Python"s and then fails
; to close them: the tray app, the server and its workers have no window to be
; asked to close. PrepareToInstall below closes them instead.
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "purgeuserdata"; Description: "Also remove user data (%AppData%\{#AppName}) on uninstall"; GroupDescription: "Uninstall behaviour:"; Flags: unchecked

[InstallDelete]
; Inno Setup only adds and overwrites. A file the previous version shipped and
; this one renamed away survives beside its replacement -- two
; kimo_cli-*.dist-info directories, say, after which
; importlib.metadata.version("kimo-cli") picks between them non-deterministically.
; build.py carries _evict_dropped_local_packages for exactly this on macOS.
; Only shipped files live here; user data is under %AppData%.
Type: filesandordirs; Name: "{app}\runtime"

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

// Counts the processes running from the install dir -- the tray app, the server,
// its workers, a CLI started from the runtime -- and with Kill, ends them. They
// hold files under {app}\runtime, which [InstallDelete] removes.
function AppProcesses(Kill: Boolean): Integer;
var
  Locator, Service, Processes, Process: Variant;
  Prefix, Path: String;
  I, Pid, ResultCode: Integer;
begin
  Result := 0;
  Prefix := Lowercase(AddBackslash(ExpandConstant('{app}')));
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Processes := Service.ExecQuery(
      'SELECT ProcessId, ExecutablePath FROM Win32_Process WHERE ExecutablePath IS NOT NULL');
    for I := 0 to Processes.Count - 1 do
    begin
      Process := Processes.ItemIndex(I);
      Path := Process.ExecutablePath;
      Pid := Process.ProcessId;
      if Pos(Prefix, Lowercase(Path)) = 1 then
      begin
        Result := Result + 1;
        if Kill then
          Exec(ExpandConstant('{sys}\taskkill.exe'), Format('/F /T /PID %d', [Pid]),
            '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    end;
  except
    Log('Could not list running processes: ' + GetExceptionMessage);
  end;
end;

// Closes a running OpenKimo, after saying so. Returns '' when none is left
// running, else why not.
function CloseRunningApp(): String;
var
  I: Integer;
begin
  Result := '';
  if AppProcesses(False) = 0 then
    Exit;
  if SuppressibleMsgBox('{#AppName} is running. Setup will close it and continue.' + #13#10#13#10 +
      'Anything it is working on right now will be stopped.',
      mbInformation, MB_OKCANCEL, IDOK) <> IDOK then
  begin
    Result := '{#AppName} is still running. Quit it from its tray icon, then run Setup again.';
    Exit;
  end;
  AppProcesses(True);
  for I := 1 to 20 do
  begin
    if AppProcesses(False) = 0 then
      Exit;
    Sleep(250);
  end;
  Result := 'Setup could not close {#AppName}. Quit it from its tray icon, or end its ' +
    'python.exe processes in Task Manager, then run Setup again.';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := CloseRunningApp();
end;

function InitializeUninstall(): Boolean;
var
  Error: String;
begin
  Error := CloseRunningApp();
  if Error <> '' then
    MsgBox(Error, mbError, MB_OK);
  Result := Error = '';
end;

// WizardIsTaskSelected only exists while the install wizard is alive, so the
// task choice is persisted to a marker file here and read back at uninstall
// (calling it from uninstall code raises "cannot call WizardIsTaskSelected").
procedure CurStepChanged(CurStep: TSetupStep);
var
  Missing: String;
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('purgeuserdata') then
      SaveStringToFile(PurgeMarkerPath(), '1', False)
    else
      DeleteFile(PurgeMarkerPath());

    // The runtime tree is ~1 GB copied file-by-file; a silent partial copy
    // (antivirus lock, disk error, a process holding a file) leaves an
    // install whose launcher starts but whose server cannot spawn — and
    // nothing says why. Verify the pieces the first run needs.
    Missing := '';
    if not FileExists(ExpandConstant('{app}\runtime\python\python.exe')) then
      Missing := Missing + '  runtime\python\python.exe' + #13#10;
    if not DirExists(ExpandConstant('{app}\runtime\playwright-browsers')) then
      Missing := Missing + '  runtime\playwright-browsers\ (the browser used to open bot-protected pages)' + #13#10;
    if Missing <> '' then
      MsgBox(
        'This installation looks INCOMPLETE — the following is missing:' + #13#10 + #13#10 +
        Missing + #13#10 +
        'The app will start but parts of it will fail (e.g. the browser ' +
        'fallback for protected web pages). Run Setup again — with antivirus ' +
        'real-time scanning temporarily off if it blocks the copy, and the app ' +
        'fully quit — or uninstall and reinstall.',
        mbError, MB_OK);
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
