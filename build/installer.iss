; orcsy Windows 安装包脚本（Inno Setup 6）
; 产物： build/Setup/orcsy-setup-<版本>.exe

#define MyAppName "Orcsy"
#define MyAppNameEn "Orcsy"
#define MyAppExeName "orcsy-web.exe"
#define MyAppNameTray "orcsy-tray.exe"
; 版本号单源化：以 orcsy/__init__.py 的 __version__ 为唯一来源，
; build/build.py 生成 build/version.ini，此处只读不再手改
#define MyAppVersion ReadIni(SourcePath + "\version.ini", "Version", "AppVersion", "0.0.0")

[Setup]
AppId={{8E4A2C6F-1B7D-4E3A-9C5F-TASKARRANGER}}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany=Orcsy
VersionInfoProductName={#MyAppNameEn}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoCopyright=Copyright (C) 2026 易开元(EOM)
AppName={#MyAppName}
; 安装时允许用户自选目录：始终显示"选择安装位置"页（默认 C:\Program Files\Orcsy，
; 已装过则预填上次目录）；静默安装可用 /DIR="D:\xxx" 指定
DisableDirPage=no
DisableProgramGroupPage=yes
DefaultDirName={autopf}\Orcsy
DefaultGroupName={#MyAppName}
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=Setup
OutputBaseFilename=orcsy-setup-{#MyAppVersion}
SetupIconFile=icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\orcsy-web.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\orcsy-tray.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; 从改名前版本（task-arranger）升级时清理旧 exe，避免残留
Type: files; Name: "{app}\task-arranger-web.exe"
Type: files; Name: "{app}\task-arranger-tray.exe"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppNameTray}"; Parameters: "--tray"; IconFilename: "{app}\{#MyAppNameTray}"; Comment: "无控制台窗口，托盘图标常驻"
Name: "{group}\{#MyAppName}（控制台窗口）"; Filename: "{app}\{#MyAppExeName}"; Parameters: "web"; IconFilename: "{app}\{#MyAppExeName}"; Comment: "带控制台输出，托盘图标常驻"
Name: "{group}\{#MyAppName}（仅启动调度，无看板）"; Filename: "{app}\{#MyAppExeName}"; Parameters: "run"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppNameTray}"; Parameters: "--tray"; IconFilename: "{app}\{#MyAppNameTray}"; Tasks: desktopicon; Comment: "无控制台窗口，托盘图标常驻"

[Tasks]
; 旧版本正在运行时才会出现这一组（Check 运行时判断）；默认勾选，安装前自动结束旧进程
Name: "closeapp"; Description: "退出正在运行的 Orcsy（结束后自动继续安装）"; GroupDescription: "检测到旧版本正在运行："; Check: IsOldAppRunning
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "autostart"; Description: "开机自动启动（可随时在托盘菜单开关）"; GroupDescription: "附加任务："; Flags: unchecked
Name: "initdata"; Description: "初始化配置与示例任务（首次安装）"; GroupDescription: "附加任务："; Flags: unchecked

[Run]
; 首次使用：在真实文档目录（与应用同源解析）下生成 config.yaml 与示例任务模板
Filename: "{app}\{#MyAppExeName}"; Parameters: "init --dir ""{code:RealUserDocs}\Orcsy"""; Tasks: initdata; Flags: runhidden
; 勾选自启动：写 HKCU Run 键（托盘 exe 静默后台）
Filename: "{app}\{#MyAppNameTray}"; Parameters: "web --tray-autostart-on"; Tasks: autostart; Flags: runhidden
Filename: "{app}\{#MyAppNameTray}"; Parameters: "--tray"; Description: "启动任务编排系统（托盘后台）"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载时清掉开机自启注册表值（Run 键本身是共享的，只删自己的值）
Filename: "{sys}\reg.exe"; Parameters: "delete ""HKCU\Software\Microsoft\Windows\CurrentVersion\Run"" /v Orcsy /f"; Flags: runhidden; RunOnceId: "DelAutoStart"

[UninstallDelete]
; 只清程序目录；用户配置与任务（文档\Orcsy）保留

[Code]
// 卸载时询问是否删除用户数据（文档\Orcsy：配置、日志、任务模板）
var DeleteUserData: Boolean;

// ===== 安装前结束正在运行的旧版本 =====

function IsOldAppRunning(): Boolean;
var
  RC: Integer;
begin
  // tasklist 对不存在的进程也返回 0，用 find 过滤后按退出码判断；
  // find/tasklist 用 System32 绝对路径，免疫安装环境的 PATH 差异
  Result := False;
  if Exec(ExpandConstant('{sys}\cmd.exe'),
      '/C tasklist /FI "IMAGENAME eq orcsy-tray.exe" | "%SystemRoot%\System32\find.exe" /I "orcsy-tray.exe" >nul',
      '', SW_HIDE, ewWaitUntilTerminated, RC) then
    Result := (RC = 0);
  if not Result then
    if Exec(ExpandConstant('{sys}\cmd.exe'),
        '/C tasklist /FI "IMAGENAME eq orcsy-web.exe" | "%SystemRoot%\System32\find.exe" /I "orcsy-web.exe" >nul',
        '', SW_HIDE, ewWaitUntilTerminated, RC) then
      Result := (RC = 0);
  // 兼容改名前的旧版本：task-arranger-* exe 仍在运行也视为旧实例
  if not Result then
    if Exec(ExpandConstant('{sys}\cmd.exe'),
        '/C tasklist /FI "IMAGENAME eq task-arranger-tray.exe" | "%SystemRoot%\System32\find.exe" /I "task-arranger-tray.exe" >nul',
        '', SW_HIDE, ewWaitUntilTerminated, RC) then
      Result := (RC = 0);
  if not Result then
    if Exec(ExpandConstant('{sys}\cmd.exe'),
        '/C tasklist /FI "IMAGENAME eq task-arranger-web.exe" | "%SystemRoot%\System32\find.exe" /I "task-arranger-web.exe" >nul',
        '', SW_HIDE, ewWaitUntilTerminated, RC) then
      Result := (RC = 0);
end;

procedure KillOldApp();
var
  RC, I: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'),
    '/f /im "orcsy-tray.exe" /im "orcsy-web.exe" /im "task-arranger-tray.exe" /im "task-arranger-web.exe"',
    '', SW_HIDE, ewWaitUntilTerminated, RC);
  // 强杀后等它真正退出（最多 5 秒），避免文件仍被占用导致覆盖失败
  for I := 1 to 10 do
  begin
    if not IsOldAppRunning() then Break;
    Sleep(500);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsOldAppRunning() then
  begin
    if WizardIsTaskSelected('closeapp') then
    begin
      KillOldApp();
      if IsOldAppRunning() then
        Result := '旧版本进程无法结束（可能被其它程序占用），请手动退出 Orcsy 后重试安装。';
    end
    else
      Result := '检测到 Orcsy 正在运行。'#13#10
        + '请勾选「退出正在运行的 Orcsy」，或手动退出旧版本后重试。';
  end;
end;

// 文档目录解析与应用（orcsy/__main__.py 的 _candidate_config_dirs）保持同源：
// 读注册表 User Shell Folders\Personal，而不是 Inno 的 {userdocs}——
// OneDrive 重定向/映射盘场景下两者可能指向不同目录，导致初始化/卸载错位
function RealUserDocs(Param: String): String;
var
  Raw: String;
begin
  Result := '';
  if RegQueryStringValue(HKEY_CURRENT_USER,
      'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders',
      'Personal', Raw) and (Trim(Raw) <> '') then
  begin
    StringChangeEx(Raw, '%USERPROFILE%', GetEnv('USERPROFILE'), True);
    if Pos('%', Raw) = 0 then
      Result := Raw;
  end;
  if Trim(Result) = '' then
    Result := ExpandConstant('{userdocs}');
end;

function ShowDataDeletePrompt: Boolean;
var
  Form: TSetupForm;
  CheckBox: TNewCheckBox;
  OKButton, CancelButton: TNewButton;
begin
  Form := CreateCustomForm(460, 170, True, False);
  try
    Form.Caption := '卸载 Orcsy';
    CheckBox := TNewCheckBox.Create(Form);
    CheckBox.Parent := Form;
    CheckBox.Top := 40;
    CheckBox.Left := 30;
    CheckBox.Width := 380;
    CheckBox.Caption := '同时删除用户数据（配置、日志、示例任务）';
    OKButton := TNewButton.Create(Form);
    OKButton.Parent := Form;
    OKButton.Left := 180;
    OKButton.Top := 90;
    OKButton.Width := 80;
    OKButton.Default := True;
    OKButton.ModalResult := mrOk;
    OKButton.Caption := '确定';
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Left := 270;
    CancelButton.Top := 90;
    CancelButton.Width := 80;
    CancelButton.Cancel := True;
    CancelButton.ModalResult := mrCancel;
    CancelButton.Caption := '取消';
    if Form.ShowModal = mrOk then
      Result := CheckBox.Checked
    else
      Result := False;
  finally
    Form.Free;
  end;
end;

function InitializeUninstall(): Boolean;
var
  Cmd: string;
begin
  DeleteUserData := False;
  Cmd := UpperCase(GetCmdTail);
  if Pos('/SILENT', Cmd) > 0 then
    DeleteUserData := False   // 静默卸载不删用户数据
  else
    DeleteUserData := ShowDataDeletePrompt;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if (CurUninstallStep = usPostUninstall) and DeleteUserData then begin
    DataDir := RealUserDocs('') + '\Orcsy';
    if DirExists(DataDir) then
      DelTree(DataDir, True, True, True);
  end;
end;
