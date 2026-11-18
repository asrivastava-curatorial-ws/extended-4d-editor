; Inno Setup script: the installer of Extended 4D Editor for Windows (64 bit).
;
;   1. build the programs:  pyinstaller --noconfirm --clean packaging\windows\extended-4d-editor.spec
;   2. build the installer: iscc /DAppVersion=3.3.0 packaging\windows\installer.iss
;      -> dist\Extended-4D-Editor-3.3.0-windows-x64-setup.exe
;
; It installs for the current user by default (no administrator rights needed) or for everybody.
; Written on Linux; NOT compiled or run by its author (docs/WINDOWS.md).

#define AppName "Extended 4D Editor"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\Extended-4D-Editor"
#endif

[Setup]
AppId={{6F0D5B1A-7C3E-4B5F-9A21-4D5E44454954}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Extended 4D Editor
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=Extended-4D-Editor-{#AppVersion}-windows-x64-setup
SetupIconFile=extended-4d-editor.ico
UninstallDisplayIcon={app}\{#AppName}.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ChangesEnvironment=yes
ChangesAssociations=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "addtopath"; Description: "Make the 'fourdvr' command known in every command prompt (adds the folder to PATH)"; Flags: unchecked
Name: "associate"; Description: "Open projects (.4dvr) with {#AppName}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{group}\fourdvr command line"; Filename: "{cmd}"; Parameters: "/K ""set ""PATH={app};%PATH%"" && fourdvr --help"""; WorkingDir: "{userdocs}"; Comment: "A command prompt in which 'fourdvr' works"
Name: "{group}\Documents"; Filename: "{app}\_internal\docs"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "{code:EnvironmentKey}"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NotOnPath
Root: HKA; Subkey: "Software\Classes\.4dvr"; ValueType: string; ValueName: ""; ValueData: "Extended4DEditor.Project"; Flags: uninsdeletevalue; Tasks: associate
Root: HKA; Subkey: "Software\Classes\Extended4DEditor.Project"; ValueType: string; ValueName: ""; ValueData: "{#AppName} project"; Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\Extended4DEditor.Project\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppName}.exe,0"; Tasks: associate
Root: HKA; Subkey: "Software\Classes\Extended4DEditor.Project\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppName}.exe"" ""%1"""; Tasks: associate

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
function EnvironmentKey(Param: String): String;
begin
  if IsAdminInstallMode then
    Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
  else
    Result := 'Environment';
end;

function NotOnPath(): Boolean;
var
  Current: String;
  Root: Integer;
begin
  if IsAdminInstallMode then
    Root := HKEY_LOCAL_MACHINE
  else
    Root := HKEY_CURRENT_USER;
  if not RegQueryStringValue(Root, EnvironmentKey(''), 'Path', Current) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(ExpandConstant('{app}')) + ';', ';' + Uppercase(Current) + ';') = 0;
end;
