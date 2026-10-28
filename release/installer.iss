; Installed edition only. The portable payload is unchanged by this wrapper.
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef PayloadDir
  #error PayloadDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef InstallerId
  #define InstallerId "D9D0E8E6-83E5-46F4-B5B1-9B3D7EE9C523"
#endif
#ifndef InstanceSuffix
  #define InstanceSuffix ""
#endif
#ifndef OutputName
  #define OutputName "Lab-Control-Center-" + AppVersion + "-windows-x64-setup"
#endif
#define ProductId "D9D0E8E6-83E5-46F4-B5B1-9B3D7EE9C523"

[Setup]
AppId={{{#InstallerId}}
AppName=Lab Control Center
AppVersion={#AppVersion}
AppPublisher=Rechoncy
AppPublisherURL=https://github.com/Rechoncy/Rigol_Generator_GUI
AppSupportURL=https://github.com/Rechoncy/Rigol_Generator_GUI/issues
DefaultDirName={localappdata}\Programs\LabControlCenter{#InstanceSuffix}
DefaultGroupName=Lab Control Center{#InstanceSuffix}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UsePreviousAppDir=yes
UsePreviousTasks=yes
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\Lab Control Center.exe
AppMutex=Local\LabControlCenter.App{#InstanceSuffix}
SetupMutex=Local\LabControlCenter.Setup{#InstanceSuffix}
CloseApplications=no
RestartApplications=no
AlwaysRestart=no
RestartIfNeededByRun=no
SetupLogging=yes
UninstallLogging=yes
Compression=lzma2/normal
SolidCompression=yes
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[CustomMessages]
english.DesktopIcon=Create a desktop shortcut
chinesesimplified.DesktopIcon=创建桌面快捷方式
english.LaunchApp=Launch Lab Control Center
chinesesimplified.LaunchApp=启动实验室设备中心
english.DataTitle=User data directory
chinesesimplified.DataTitle=用户数据目录
english.DataSubtitle=Keep settings and experiment data outside the program directory.
chinesesimplified.DataSubtitle=将设备配置与实验数据保存在程序目录之外。
english.DataHint=A verified backup is made before removing a previous installed version. Existing data is preserved during installation and uninstallation. For a first migration from a portable version, choose that version's data directory.
chinesesimplified.DataHint=卸载旧版本前会先创建并校验数据备份；安装、升级和普通卸载均保留用户数据。首次从便携版迁移时，可选择原便携版的 data 目录。
english.DataLabel=User data:
chinesesimplified.DataLabel=用户数据：
english.AbsoluteData=Choose an absolute user data directory.
chinesesimplified.AbsoluteData=请选择用户数据目录的完整绝对路径。
english.DataSubdirectory=Choose an absolute user data subdirectory, not a drive root.
chinesesimplified.DataSubdirectory=请选择专用的用户数据子目录，不可使用磁盘或共享盘根目录。
english.SeparateData=The user data and program directories must be separate.
chinesesimplified.SeparateData=用户数据目录与程序目录必须相互独立，不能互相包含。
english.DataOutsideOld=Move user data outside the previous program directory before upgrading.
chinesesimplified.DataOutsideOld=请先将用户数据迁移到旧程序目录以外，再继续升级。
english.OldRegistration=The previous uninstall registration is incomplete. No program or user data was removed.
chinesesimplified.OldRegistration=旧版本卸载信息不完整，已停止安装；未移除旧程序或用户数据。
english.SeparateBackup=The backup, program and user data directories must be separate.
chinesesimplified.SeparateBackup=备份目录必须位于程序目录和用户数据目录之外，且不能互相包含。
english.BackupDirectory=Cannot create a new backup directory. No previous version was removed.
chinesesimplified.BackupDirectory=无法创建新的备份目录；尚未卸载旧版本。
english.BackupHelper=Cannot prepare the bundled data backup helper. No previous version was removed.
chinesesimplified.BackupHelper=无法准备内置数据备份程序；尚未卸载旧版本。
english.BackupFailed=User data backup failed. The previous version has not been removed. Details:
chinesesimplified.BackupFailed=用户数据备份失败，已停止升级，尚未卸载旧版本。诊断目录：
english.BackupMissing=A verified data backup could not be identified. The previous version has not been removed.
chinesesimplified.BackupMissing=未找到唯一且有效的已校验备份，已停止升级，尚未卸载旧版本。
english.UninstallFailed=The previous version could not be uninstalled. User data and backup are retained:
chinesesimplified.UninstallFailed=旧版本卸载失败；已保留用户数据和备份，诊断目录：
english.UninstallIncomplete=The previous uninstall did not finish. User data and backup are retained:
chinesesimplified.UninstallIncomplete=旧版本尚未完成卸载，已停止安装；已保留用户数据和备份，诊断目录：
english.PendingMarker=Cannot mark installation as pending. User data and verified backup are retained:
chinesesimplified.PendingMarker=无法记录安装状态，已停止安装；已保留用户数据和校验后的备份，诊断目录：
english.RestoreFailed=User data restore/verification failed. Do not start the application. User data and verified backup remain at:
chinesesimplified.RestoreFailed=用户数据恢复或校验失败，请勿启动程序；已保留用户数据和已校验备份，诊断目录：
english.InstalledMarker=Cannot record the installed data directory. Backup retained:
chinesesimplified.InstalledMarker=无法记录安装后的数据目录，已保留备份，诊断目录：
english.FinishMarker=Cannot finish the installation marker. Backup retained:
chinesesimplified.FinishMarker=无法完成安装状态记录，已保留备份，诊断目录：

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked

[Files]
; No user data, logs or update downloads may be part of an uninstall file list.
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Excludes: "data\*,runtime-data\*,logs\*,updates\*,installed-mode.json,unins*.exe,unins*.dat"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Lab Control Center{#InstanceSuffix}"; Filename: "{app}\Lab Control Center.exe"; Parameters: "--data-dir ""{code:GetUserDataDir}"""; WorkingDir: "{app}"
Name: "{autodesktop}\Lab Control Center{#InstanceSuffix}"; Filename: "{app}\Lab Control Center.exe"; Parameters: "--data-dir ""{code:GetUserDataDir}"""; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; Retained across ordinary uninstall, so reinstall can find a custom data path.
Root: HKCU; Subkey: "Software\Rechoncy\LabControlCenter{#InstanceSuffix}"; ValueType: string; ValueName: "UserDataDir"; ValueData: "{code:GetUserDataDir}"

[UninstallDelete]
; These two files belong to this installer. Never delete a user data directory.
Type: files; Name: "{app}\installed-mode.json"
Type: files; Name: "{app}\install-pending.json"

[Run]
Filename: "{app}\Lab Control Center.exe"; Parameters: "--data-dir ""{code:GetUserDataDir}"""; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent; Check: MayAutoRun

; User data and backups are external. Program cleanup is restricted to files
; tracked by [Files] and the two exact installer-owned markers above.
[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#InstallerId}}_is1';
  PersistentKey = 'Software\Rechoncy\LabControlCenter{#InstanceSuffix}';
  ProductId = '{{#ProductId}}';

var
  DataPage: TInputDirWizardPage;
  DataDirectory: String;
  BackupDirectory: String;
  BackupParent: String;
  OldInstallDirectory: String;
  OldUninstaller: String;
  Prepared: Boolean;
  Restored: Boolean;

function GetTickCount: LongWord;
  external 'GetTickCount@kernel32.dll stdcall';

function Quoted(const Value: String): String;
begin
  Result := '"' + Value + '"';
end;

function NormalPath(const Value: String): String;
begin
  Result := RemoveBackslashUnlessRoot(ExpandFileName(Value));
end;

function AtOrBelow(const Child, Parent: String): Boolean;
var
  ParentWithSlash, ChildWithSlash: String;
begin
  ParentWithSlash := AddBackslash(NormalPath(Parent));
  ChildWithSlash := AddBackslash(NormalPath(Child));
  Result := CompareText(Copy(ChildWithSlash, 1, Length(ParentWithSlash)), ParentWithSlash) = 0;
end;

function GetUserDataDir(Param: String): String;
begin
  Result := DataDirectory;
end;

function GetBackupDir(Param: String): String;
begin
  Result := BackupDirectory;
end;

function MayAutoRun: Boolean;
begin
  Result := Restored and (ExpandConstant('{param:NOAUTORUN|0}') = '0');
end;

function UninstallExecutable(const Value: String): String;
var
  ClosingQuote: Integer;
begin
  Result := Trim(Value);
  if Copy(Result, 1, 1) = '"' then begin
    Delete(Result, 1, 1);
    // Keep the index in Pascal Script string-character units. Pos() may use
    // an ANSI overload, whose byte offset differs for non-ASCII app paths.
    ClosingQuote := 1;
    while (ClosingQuote <= Length(Result)) and (Result[ClosingQuote] <> '"') do
      ClosingQuote := ClosingQuote + 1;
    if ClosingQuote > Length(Result) then begin
      Result := '';
      Exit;
    end;
    Result := Copy(Result, 1, ClosingQuote - 1);
  end;
end;

procedure InitializeWizard;
var
  PreviousData, PreviousUninstall: String;
begin
  if not RegQueryStringValue(HKCU, UninstallKey, 'UserDataDir', PreviousData) or
     (Trim(PreviousData) = '') then begin
    if not RegQueryStringValue(HKCU, PersistentKey, 'UserDataDir', PreviousData) or
       (Trim(PreviousData) = '') then
      PreviousData := ExpandConstant('{localappdata}\RigolGeneratorControl\data');
  end;
  // A remembered user path is opaque data, not part of an Inno constant
  // expression (a legal Windows directory name may contain literal braces).
  DataDirectory := ExpandConstant('{param:DATADIR|}');
  if Trim(DataDirectory) = '' then DataDirectory := PreviousData;
  DataDirectory := NormalPath(DataDirectory);
  OldInstallDirectory := '';
  OldUninstaller := '';
  if RegQueryStringValue(HKCU, UninstallKey, 'InstallLocation', OldInstallDirectory) then begin
    OldInstallDirectory := NormalPath(OldInstallDirectory);
    if RegQueryStringValue(HKCU, UninstallKey, 'UninstallString', PreviousUninstall) then
      OldUninstaller := UninstallExecutable(PreviousUninstall);
  end;
  DataPage := CreateInputDirPage(wpSelectDir, CustomMessage('DataTitle'),
    CustomMessage('DataSubtitle'), CustomMessage('DataHint'),
    False, '');
  DataPage.Add(CustomMessage('DataLabel'));
  DataPage.Values[0] := DataDirectory;
end;

function ValidateDirectories: String;
var
  InstallDir: String;
begin
  Result := '';
  if (Trim(DataPage.Values[0]) = '') or (ExtractFileDrive(DataPage.Values[0]) = '') then begin
    Result := CustomMessage('AbsoluteData');
    Exit;
  end;
  DataDirectory := NormalPath(DataPage.Values[0]);
  InstallDir := NormalPath(ExpandConstant('{app}'));
  if (ExtractFileDrive(DataDirectory) = '') or
     (CompareText(DataDirectory, AddBackslash(ExtractFileDrive(DataDirectory))) = 0) then begin
    Result := CustomMessage('DataSubdirectory');
    Exit;
  end;
  if AtOrBelow(DataDirectory, InstallDir) or AtOrBelow(InstallDir, DataDirectory) then begin
    Result := CustomMessage('SeparateData');
    Exit;
  end;
  if (OldInstallDirectory <> '') and
     (AtOrBelow(DataDirectory, OldInstallDirectory) or AtOrBelow(OldInstallDirectory, DataDirectory)) then begin
    Result := CustomMessage('DataOutsideOld');
    Exit;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Error: String;
begin
  Result := True;
  if CurPageID = DataPage.ID then begin
    Error := ValidateDirectories;
    if Error <> '' then begin
      SuppressibleMsgBox(Error, mbError, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;

function FindSnapshot(const Parent: String): String;
var
  Found: TFindRec;
  Candidate: String;
  Count: Integer;
begin
  Result := '';
  Count := 0;
  if FindFirst(AddBackslash(Parent) + 'backup-*', Found) then begin
    try
      repeat
        if (Found.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then begin
          Candidate := AddBackslash(Parent) + Found.Name;
          if FileExists(AddBackslash(Candidate) + 'manifest.json') then begin
            Count := Count + 1;
            Result := Candidate;
          end;
        end;
      until not FindNext(Found);
    finally
      FindClose(Found);
    end;
  end;
  if Count <> 1 then Result := '';
end;

function HelperExecutable: String;
begin
  Result := ExpandConstant('{tmp}\') + '{app}\Lab Control Center.exe';
end;

function IsPreviousApplication: Boolean;
var
  Marker: AnsiString;
begin
  Result := False;
  if LoadStringFromFile(AddBackslash(OldInstallDirectory) + 'installed-mode.json', Marker) then begin
    Log('Previous marker format position: ' + IntToStr(Pos('"format":"lab-control-center-installed"', String(Marker))));
    Log('Previous marker identity position: ' + IntToStr(Pos('"app_id":"' + ProductId + '"', String(Marker))));
    Result := (Pos('"format":"lab-control-center-installed"', String(Marker)) > 0) and
      (Pos('"app_id":"' + ProductId + '"', String(Marker)) > 0);
  end else
    Log('Previous installed marker could not be read.');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  BackupBase, Parameters, RegistrationProblem: String;
  ExitCode, Attempts: Integer;
begin
  Result := '';
  if Prepared then Exit;
  Result := ValidateDirectories;
  if Result <> '' then Exit;
  if OldInstallDirectory <> '' then begin
    Log('Previous registered directory: ' + OldInstallDirectory);
    Log('Previous registered uninstaller: ' + OldUninstaller);
    RegistrationProblem := '';
    if OldUninstaller = '' then RegistrationProblem := 'empty uninstaller'
    else if not FileExists(OldUninstaller) then RegistrationProblem := 'uninstaller missing'
    else if CompareText(NormalPath(ExtractFileDir(OldUninstaller)), OldInstallDirectory) <> 0 then
      RegistrationProblem := 'uninstaller directory differs'
    else if CompareText(Copy(ExtractFileName(OldUninstaller), 1, 5), 'unins') <> 0 then
      RegistrationProblem := 'uninstaller filename is not owned'
    else if CompareText(ExtractFileExt(OldUninstaller), '.exe') <> 0 then
      RegistrationProblem := 'uninstaller extension is not exe'
    else if not IsPreviousApplication then RegistrationProblem := 'installed marker differs';
    if RegistrationProblem <> '' then begin
      Log('Previous registration validation failed: ' + RegistrationProblem);
      Result := CustomMessage('OldRegistration');
      Exit;
    end;
  end;
  BackupBase := NormalPath(ExpandConstant('{param:BACKUPROOT|{localappdata}\RigolGeneratorControl\installer-backups}'));
  if AtOrBelow(BackupBase, DataDirectory) or AtOrBelow(DataDirectory, BackupBase) or
     AtOrBelow(BackupBase, ExpandConstant('{app}')) or
     ((OldInstallDirectory <> '') and AtOrBelow(BackupBase, OldInstallDirectory)) then begin
    Result := CustomMessage('SeparateBackup');
    Exit;
  end;
  BackupParent := AddBackslash(BackupBase) + 'install-' +
    GetDateTimeString('yyyymmdd-hhnnss', '-', ':') + '-' + IntToStr(GetTickCount);
  if DirExists(BackupParent) or not ForceDirectories(BackupParent) then begin
    Result := CustomMessage('BackupDirectory');
    Exit;
  end;
  Log('Preparing verified user data backup: ' + BackupParent);
  try
    ExtractTemporaryFiles('{app}\*');
  except
    Result := CustomMessage('BackupHelper');
    Exit;
  end;
  Parameters := '--installer-data-backup ' + Quoted(DataDirectory) +
    ' --backup-root ' + Quoted(BackupParent) +
    ' --report ' + Quoted(AddBackslash(BackupParent) + 'backup-report.json');
  if not Exec(HelperExecutable, Parameters, ExtractFileDir(HelperExecutable),
              SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then begin
    Result := CustomMessage('BackupFailed') + ' ' + BackupParent;
    Exit;
  end;
  BackupDirectory := FindSnapshot(BackupParent);
  if BackupDirectory = '' then begin
    Result := CustomMessage('BackupMissing');
    Exit;
  end;
  if OldInstallDirectory <> '' then begin
    Log('Removing only the previous registered program: ' + OldInstallDirectory);
    Parameters := '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=' +
      Quoted(AddBackslash(BackupParent) + 'previous-uninstall.log');
    if not Exec(OldUninstaller, Parameters, OldInstallDirectory,
                SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then begin
      Result := CustomMessage('UninstallFailed') + ' ' + BackupParent;
      Exit;
    end;
    // Inno's original uninstaller can exit before its temporary worker has
    // removed the executable. Wait for owned files AND registration to clear.
    Attempts := 0;
    while ((FileExists(OldUninstaller) or RegKeyExists(HKCU, UninstallKey)) and (Attempts < 300)) do begin
      Sleep(100);
      Attempts := Attempts + 1;
    end;
    if FileExists(OldUninstaller) or RegKeyExists(HKCU, UninstallKey) then begin
      Result := CustomMessage('UninstallIncomplete') + ' ' + BackupParent;
      Exit;
    end;
    Log('Previous registered program removed; user data directory retained.');
  end;
  if not ForceDirectories(ExpandConstant('{app}')) or
     not SaveStringToFile(ExpandConstant('{app}\install-pending.json'),
       '{"format":"lab-control-center-install-pending","schema_version":1}', False) then begin
    Result := CustomMessage('PendingMarker') + ' ' + BackupParent;
    Exit;
  end;
  Prepared := True;
end;

function JsonString(const Value: String): String;
begin
  Result := Value;
  StringChangeEx(Result, '\', '\\', True);
  StringChangeEx(Result, '"', '\"', True);
  Result := '"' + Result + '"';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Parameters, Marker: String;
  ExitCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    Parameters := '--installer-data-restore ' + Quoted(DataDirectory) +
      ' --backup ' + Quoted(BackupDirectory) +
      ' --report ' + Quoted(AddBackslash(BackupParent) + 'restore-report.json');
    if not Exec(HelperExecutable, Parameters, ExtractFileDir(HelperExecutable),
                SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then
      RaiseException(CustomMessage('RestoreFailed') + ' ' + BackupParent);
    Marker := '{"format":"lab-control-center-installed","schema_version":1,' +
      '"app_id":"' + ProductId + '","instance_suffix":"{#InstanceSuffix}","data_dir":' +
      JsonString(DataDirectory) + '}';
    if not SaveStringToFile(ExpandConstant('{app}\installed-mode.json'), UTF8Encode(Marker), False) then
      RaiseException(CustomMessage('InstalledMarker') + ' ' + BackupParent);
    // Inno recreates its uninstall key after processing [Registry]. Record
    // custom metadata only after that standard key has been saved.
    if not RegWriteStringValue(HKCU, UninstallKey, 'UserDataDir', DataDirectory) or
       not RegWriteStringValue(HKCU, UninstallKey, 'LastDataBackup', BackupDirectory) then
      RaiseException(CustomMessage('InstalledMarker') + ' ' + BackupParent);
    if not DeleteFile(ExpandConstant('{app}\install-pending.json')) then
      RaiseException(CustomMessage('FinishMarker') + ' ' + BackupParent);
    Restored := True;
    Log('User data restored and verified. Backup retained: ' + BackupDirectory);
  end;
end;
