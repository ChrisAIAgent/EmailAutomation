; Email Automation - Windows installer (Inno Setup 6)
; Build:  powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1 -Version "1.0.0"
;         (which stages the payload, generates the icon, injects the version
;          and calls ISCC on this file)

#define MyAppName "Email Automation"
#define MyAppVersion "1.2.4"
#define MyAppPublisher "TAC AISolution"
#define MyAppId "B8C9D0F2-2E6D-4C89-9A1D-EMAILAUTOMATION"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName} installer
VersionInfoCopyright=Copyright (C) TAC AISolution
DefaultDirName={autopf}\TAC AISolution\Email Automation
DefaultGroupName={#MyAppName}
OutputDir=..\dist
OutputBaseFilename=Email-Automation-Setup-{#MyAppVersion}
Compression=lzma2/fast
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupIconFile=assets\EmailAutomation.ico
UninstallDisplayIcon={app}\branding\EmailAutomation.ico
DisableProgramGroupPage=yes
; Reserve the install dir; do not require an empty folder.
DirExistsWarning=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
; The build script stages the sanitized workspace into installer\payload.
; The build script stages the sanitized workspace into installer\payload.
; Excludes "*.docx" because no deliverable .docx exists; this also robustly
; drops the Unicode-named dev artifact (截图.docx) that robocopy /XF cannot
; match on its own.
Source: "payload\*"; DestDir: "{app}"; Excludes: "*.docx"; Flags: recursesubdirs createallsubdirs ignoreversion
; Official brand assets for the installed Web UI and shortcuts.
Source: "..\TACWork-Logo-Black.PNG"; DestDir: "{app}\branding"; Flags: ignoreversion
Source: "..\frontend\public\tac-logo.png"; DestDir: "{app}\branding"; Flags: ignoreversion
; Installer icon (also used as the uninstall icon).
Source: "assets\EmailAutomation.ico"; DestDir: "{app}\branding"; Flags: ignoreversion
; The official, pinned Microsoft x64 redistributable is validated by
; scripts\verify-vc-redist.ps1 before ISCC runs. Keep it app-local so upgrades
; and repair installs remain offline and do not require a customer download.
Source: "prerequisites\vc_redist.x64.exe"; DestDir: "{app}\prerequisites"; Flags: ignoreversion
; A release-specific, explicit upgrade cleanup list. It is embedded rather than
; read from disk at install time, and only relative {app} paths are accepted.
Source: "obsolete-files-1.2.4.txt"; Flags: dontcopy

[Dirs]
; The program directory ({app}) is kept read-only. All mutable data (DB, queue,
; logs, .env, keys, TACWork session) is created under %LOCALAPPDATA% on first
; launch (see backend/app/config.py and scripts/data-dir.ps1), never under {app}.
; No [Dirs] entries are needed here because the first launch creates them.

[Icons]
; Start-menu group
Name: "{group}\{#MyAppName}"; Filename: "{app}\runtime\electron\Email Automation.exe"; WorkingDir: "{app}"
Name: "{group}\停止 {#MyAppName}"; Filename: "{app}\stop-stack.bat"; WorkingDir: "{app}"
Name: "{group}\健康检查"; Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\agent-health.ps1"""; WorkingDir: "{app}"
; Desktop (optional task)
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\runtime\electron\Email Automation.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Optional launch after install. start-stack.bat starts only the prebuilt
; runtime and opens Web Setup/Dashboard after every service is healthy.
; The installer never triggers Gmail sync / drafts / approvals / sends / automations.
Filename: "{app}\runtime\electron\Email Automation.exe"; Description: "Launch {#MyAppName}"; Flags: postinstall nowait skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  UninstallKey, UninstallString: string;
  ResultCode: Integer;
  RepairDir, RepairExe: string;
begin
  // Upgrade path: stop the running stack before files are replaced. This MUST
  // run from CurStepChanged (NOT InitializeSetup) because {app} is only valid
  // after the wizard has resolved the destination directory. Calling
  // ExpandConstant('{app}') inside InitializeSetup raises
  // "An attempt was made to expand the 'app' constant before it was initialized"
  // and aborts the install mid-wizard. ssInstall fires after the user clicks
  // Install (the destination is resolved, so {app} is valid) but before any
  // file extraction, so we stop the old stack before its files are replaced.
  if CurStep = ssInstall then begin
    UninstallKey := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#MyAppId}_is1';
    if (RegQueryStringValue(HKLM, UninstallKey, 'UninstallString', UninstallString)) or
       (RegQueryStringValue(HKCU, UninstallKey, 'UninstallString', UninstallString)) then begin
      if FileExists(ExpandConstant('{app}\stop-stack.bat')) then begin
        Exec('cmd.exe', '/c "{app}\stop-stack.bat"', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    end;
    RemoveObsoleteFiles();
  end;
  if CurStep = ssPostInstall then begin
    InstallVcRedist();
    RepairDir := ExpandConstant('{app}\repair');
    RepairExe := RepairDir + '\Email-Automation-Repair.exe';
    ForceDirectories(RepairDir);
    CopyFile(ExpandConstant('{srcexe}'), RepairExe, False);
  end;
end;

procedure RemoveObsoleteFiles();
var
  Lines: TArrayOfString;
  I: Integer;
  Rel, Target, AppRoot: string;
begin
  ExtractTemporaryFile('obsolete-files-1.2.4.txt');
  if not LoadStringsFromFile(ExpandConstant('{tmp}\obsolete-files-1.2.4.txt'), Lines) then
    RaiseException('Unable to load the release obsolete-file list.');
  AppRoot := AddBackslash(ExpandConstant('{app}'));
  for I := 0 to GetArrayLength(Lines) - 1 do begin
    Rel := Trim(Lines[I]);
    if (Rel = '') or (Rel[1] = '#') then continue;
    StringChangeEx(Rel, '/', '\\', True);
    if (Pos('..', Rel) > 0) or (Pos(':', Rel) > 0) or (Copy(Rel, 1, 1) = '\\') then
      RaiseException('Unsafe obsolete-file path in release list: ' + Rel);
    Target := AppRoot + Rel;
    if FileExists(Target) then DeleteFile(Target)
    else if DirExists(Target) then DelTree(Target, False, True, False);
  end;
end;

procedure InstallVcRedist();
var
  ResultCode: Integer;
  Redist: string;
begin
  Redist := ExpandConstant('{app}\prerequisites\vc_redist.x64.exe');
  if not FileExists(Redist) then
    RaiseException('Bundled Microsoft Visual C++ x64 runtime is missing.');
  if not Exec(Redist, '/install /quiet /norestart', ExpandConstant('{app}'), SW_HIDE,
              ewWaitUntilTerminated, ResultCode) then
    RaiseException('Could not start the bundled Microsoft Visual C++ x64 runtime installer.');
  // 0 = installed, 1638 = a newer version is already installed,
  // 3010 = installed and a restart is recommended. All preserve launch safety.
  if (ResultCode <> 0) and (ResultCode <> 1638) and (ResultCode <> 3010) then
    RaiseException('Bundled Microsoft Visual C++ x64 runtime installation failed (exit ' + IntToStr(ResultCode) + ').');
end;
function IsSilentUninstall(): Boolean;
var
  i: Integer;
  s: string;
begin
  Result := False;
  for i := 1 to ParamCount do begin
    s := ParamStr(i);
    if (CompareText(s, '/SILENT') = 0) or (CompareText(s, '/VERYSILENT') = 0) then
      Result := True;
  end;
end;

procedure CurUninstallStepChanged(CurStep: TUninstallStep);
var
  Msg, Paths, LocalDataDir: string;
begin
  // Only prompt in INTERACTIVE uninstall. In silent (/VERYSILENT) mode we keep
  // ALL business data, config and logs by default - deletion is never implicit.
  if (CurStep = usPostUninstall) and (not IsSilentUninstall()) then begin
    LocalDataDir := ExpandConstant('{localappdata}') + '\TAC AISolution\Email Automation';
    Paths := LocalDataDir + #13#10 +
             LocalDataDir + '\database (app.db*)' + #13#10 +
             LocalDataDir + '\queue (huey.db, consumer-status.json)' + #13#10 +
             LocalDataDir + '\logs' + #13#10 +
             LocalDataDir + '\config (.env, security.json)';
    Msg := '是否删除业务数据与日志？' + #13#10#13#10 +
           '默认保留以下目录与文件（推荐升级时保留）：' + #13#10 + Paths;
    if MsgBox(Msg, mbConfirmation, MB_YESNO) = IDYES then begin
      if MsgBox('即将删除以下业务数据与日志，此操作不可逆，确认删除吗？' + #13#10#13#10 + Paths,
                mbCriticalError, MB_YESNO) = IDYES then begin
        DelTree(LocalDataDir, True, True, True);
      end;
    end;
  end;
end;
