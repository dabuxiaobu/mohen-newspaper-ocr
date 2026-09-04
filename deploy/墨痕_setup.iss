; ============================================================================
; 墨痕 · 近代报刊转录助手 —— Inno Setup 安装脚本
; ----------------------------------------------------------------------------
; 用法（在你自己的 Windows 上）：
;   1. 安装 Inno Setup（https://jrsoftware.org/isdl.php，免费）。
;   2. 先用本技能的 build_exe.bat 完整重打包，确保 dist\墨痕\ 是最新修复版。
;   3. 右键本文件 →「Compile」，或命令行：iscc 墨痕_setup.iss
;   4. 生成的 setup.exe 在 deploy\Output\ 下，发给朋友双击即可安装。
;
; ★ 红线：本脚本采用「白名单」策略，[Files] 段只列两样东西：
;       · dist\墨痕\墨痕.exe
;       · dist\墨痕\_internal\*（程序依赖，无敏感数据）
;   绝不打包 box_config.json（含 apikey）、cropped_hi/、output/、source/、
;   *.log、测试图片等。任何未显式列出的文件都不会进安装包。
;   原始 dist\墨痕\box_config.json 一字不改、绝不覆盖/删除。
;
; ★ 安装位置：用户目录（C:\Users\你\AppData\Local\Programs\墨痕），
;   免 UAC 提权；运行时数据（output/、cropped_hi/）直接写 exe 同目录，
;   普通用户有完全写权限，无需改任何代码。
; ============================================================================

#define MyAppName "墨痕·近代报刊转录助手"
#define MyAppVersion "1.2.1"
#define MyPublisher "墨痕"
; dist 路径：相对本 .iss（技能根/deploy/）→ 技能根/dist/墨痕/
#define DistDir "..\dist\墨痕"

[Setup]
AppId=MohenOCR20260826
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyPublisher}
DefaultDirName={localappdata}\Programs\墨痕
DefaultGroupName={#MyAppName}
; 用户级安装，免 UAC 提权（运行时需往安装目录写 output/cropped_hi）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
OutputDir=Output
OutputBaseFilename=墨痕-1.2.1-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
WizardResizable=yes
ArchitecturesInstallIn64BitMode=x64
SetupMutex=墨痕SetupMutex
; 卸载程序信息
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\墨痕.exe
; 安装包图标与程序 exe 保持一致（skill 根 /icon/newspaper.ico）
SetupIconFile=..\icon\newspaper.ico

[Languages]
; 简体中文向导（ChineseSimplified.isl 已置于本 .iss 同目录）
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Files]
; —— 白名单：仅以下两项进入安装包 ——
Source: "{#DistDir}\墨痕.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#DistDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

; —— 可选：WebView2 引导安装包 ——
; 若本目录存在 MicrosoftEdgeWebview2Setup.exe（从微软官网下载，约 1.5MB），
; 则一并编译进安装包，在 [Code] 中检测缺失时静默安装。
; 下载地址：https://go.microsoft.com/fwlink/p/?LinkId=2124703
#ifexist "MicrosoftEdgeWebview2Setup.exe"
Source: "MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\墨痕.exe"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\墨痕.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Run]
Filename: "{app}\墨痕.exe"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall

[Code]
// 检测系统是否已安装 Microsoft Edge WebView2 Runtime
function IsWebView2Installed(): Boolean;
var
  Key: string;
begin
  Key := 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := RegKeyExists(HKEY_LOCAL_MACHINE, Key) or RegKeyExists(HKEY_CURRENT_USER, Key);
end;

function InitializeSetup(): Boolean;
var
  Code: Integer;
begin
  Result := True;
#ifexist "MicrosoftEdgeWebview2Setup.exe"
  if not IsWebView2Installed() then begin
    if MsgBox('本程序需要 Microsoft Edge WebView2 Runtime 才能运行。' + #13#10 +
              '是否现在下载并安装？（需联网，约数十 MB）',
              mbConfirmation, MB_YESNO) = IDYES then begin
      ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
      if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
                  '--silent --accept-eula --do-not-launch-msedge',
                  '', SW_HIDE, ewWaitUntilTerminated, Code) then begin
        MsgBox('WebView2 安装程序启动失败，请手动安装后重试。', mbError, MB_OK);
        Result := False;
      end;
    end else
      MsgBox('未安装 WebView2 Runtime，程序可能无法启动。', mbInformation, MB_OK);
  end;
#endif
end;
