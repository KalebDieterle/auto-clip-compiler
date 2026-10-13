; Inno Setup script for Auto Clip Compiler. Built by packaging\build.ps1 (passes /DAppVersion=...).
; Per-user install: no admin prompt; installs to %LOCALAPPDATA%\Programs\Auto Clip Compiler.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Auto Clip Compiler"
#define AppId_ "KalebDieterle.AutoClipCompiler"
; No exe of our own: Python's signed pythonw.exe runs the app (Smart App Control friendly).
#define Launcher "{app}\python\pythonw.exe"
#define LaunchArgs "-m clipcompiler.ui.main"

[Setup]
AppId={{6B1F7E2C-3D84-4C1B-9A57-2E0C9D5A41F3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Kaleb Dieterle
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=AutoClipCompiler-Setup-{#AppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\app.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\AutoClipCompiler\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; An upgrade replaces the whole app; stale modules must not linger.
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\site-packages"

[UninstallDelete]
; Python writes __pycache__ folders at runtime.
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\site-packages"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{#Launcher}"; Parameters: "{#LaunchArgs}"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; AppUserModelID: "{#AppId_}"
Name: "{autodesktop}\{#AppName}"; Filename: "{#Launcher}"; Parameters: "{#LaunchArgs}"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; AppUserModelID: "{#AppId_}"; Tasks: desktopicon

[Run]
Filename: "{#Launcher}"; Parameters: "{#LaunchArgs}"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2Key = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasWebView2: Boolean;
var
  Version: String;
begin
  Result := (RegQueryStringValue(HKLM, WebView2Key, 'pv', Version) or
             RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version))
            and (Version <> '') and (Version <> '0.0.0.0');
end;

function InitializeSetup: Boolean;
var
  ErrorCode: Integer;
begin
  Result := True;
  if not HasWebView2 then
  begin
    if MsgBox('Auto Clip Compiler needs the Microsoft Edge WebView2 Runtime, which is not installed on this PC.' + #13#10#13#10 +
              'Open the download page now? Install it, then run this setup again.', mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://developer.microsoft.com/microsoft-edge/webview2/', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
    Result := False;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    if MsgBox('Also delete your settings, saved Gemini judgments and the stored API key?' + #13#10#13#10 +
              'Keep them if you might reinstall: saved judgments are never paid for again. ' +
              'Your clips and finished compilations are never touched either way.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DelTree(ExpandConstant('{localappdata}\AutoClipCompiler'), True, True, True);
      DelTree(ExpandConstant('{userappdata}\AutoClipCompiler'), True, True, True);
      Exec('cmdkey.exe', '/delete:AutoClipCompiler', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Exec('cmdkey.exe', '/delete:gemini-api-key@AutoClipCompiler', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
end;
