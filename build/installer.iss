; task-arranger Windows 安装包脚本（Inno Setup 6）
; 产物： build/Setup/task-arranger-setup-<版本>.exe

#define MyAppName "任务编排系统"
#define MyAppNameEn "Task Arranger"
#define MyAppExeName "task-arranger-web.exe"
#define MyAppNameTray "task-arranger-tray.exe"
#define MyAppVersion "0.4.1"

[Setup]
AppId={{8E4A2C6F-1B7D-4E3A-9C5F-TASKARRANGER}}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany=TaskArranger
VersionInfoProductName={#MyAppNameEn}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoCopyright=Copyright (C) 2026 TaskArranger
AppName={#MyAppName}
; 安装时允许用户自选目录：始终显示"选择安装位置"页（默认 C:\Program Files\TaskArranger，
; 已装过则预填上次目录）；静默安装可用 /DIR="D:\xxx" 指定
DisableDirPage=no
DisableProgramGroupPage=yes
DefaultDirName={autopf}\TaskArranger
DefaultGroupName={#MyAppName}
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=Setup
OutputBaseFilename=task-arranger-setup-{#MyAppVersion}
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
Source: "..\dist\task-arranger-web.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\task-arranger-tray.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppNameTray}"; Parameters: "--tray --config ""{userdocs}\TaskArranger\config.yaml"""; IconFilename: "{app}\{#MyAppNameTray}"; Comment: "静默后台运行，系统托盘操作"
Name: "{group}\{#MyAppName}（控制台模式）"; Filename: "{app}\{#MyAppExeName}"; Parameters: "web --config ""{userdocs}\TaskArranger\config.yaml"""; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName}（仅启动调度，无看板）"; Filename: "{app}\{#MyAppExeName}"; Parameters: "run --config ""{userdocs}\TaskArranger\config.yaml"""
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppNameTray}"; Parameters: "--tray --config ""{userdocs}\TaskArranger\config.yaml"""; IconFilename: "{app}\{#MyAppNameTray}"; Tasks: desktopicon; Comment: "静默后台运行，系统托盘操作"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式（托盘模式）"; GroupDescription: "附加任务："
Name: "autostart"; Description: "开机自动启动（托盘模式，可随时在托盘菜单开关）"; GroupDescription: "附加任务："; Flags: unchecked
Name: "initdata"; Description: "初始化配置与示例任务（首次安装）"; GroupDescription: "附加任务："; Flags: unchecked

[Run]
; 首次使用：在 文档\TaskArranger 下生成 config.yaml 与示例任务模板
Filename: "{app}\{#MyAppExeName}"; Parameters: "init --dir ""{userdocs}\TaskArranger"""; Tasks: initdata; Flags: runhidden
; 勾选自启动：写 HKCU Run 键（托盘 exe 静默后台）
Filename: "{app}\{#MyAppNameTray}"; Parameters: "web --tray-autostart-on"; Tasks: autostart; Flags: runhidden
Filename: "{app}\{#MyAppNameTray}"; Parameters: "--tray --config ""{userdocs}\TaskArranger\config.yaml"""; Description: "启动任务编排系统（托盘后台）"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载时清掉开机自启注册表值（Run 键本身是共享的，只删自己的值）
Filename: "{sys}\reg.exe"; Parameters: "delete ""HKCU\Software\Microsoft\Windows\CurrentVersion\Run"" /v TaskArranger /f"; Flags: runhidden; RunOnceId: "DelAutoStart"

[UninstallDelete]
; 只清程序目录；用户配置与任务（文档\TaskArranger）保留

[Code]
// 卸载时询问是否删除用户数据（文档\TaskArranger：配置、日志、任务模板）
var DeleteUserData: Boolean;

function ShowDataDeletePrompt: Boolean;
var
  Form: TSetupForm;
  CheckBox: TNewCheckBox;
  OKButton, CancelButton: TNewButton;
begin
  Form := CreateCustomForm(460, 170, True, False);
  try
    Form.Caption := '卸载 任务编排系统';
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
    DataDir := ExpandConstant('{userdocs}') + '\TaskArranger';
    if DirExists(DataDir) then
      DelTree(DataDir, True, True, True);
  end;
end;
