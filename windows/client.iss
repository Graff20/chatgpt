[Setup]
AppId=CehClient
AppName=Цех — рабочее место
AppVersion=0.6.0
DefaultDirName={localappdata}\Programs\CehClient
DefaultGroupName=Цех
PrivilegesRequired=lowest
OutputDir=..\dist\installers
OutputBaseFilename=CehClientSetup
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
[Files]
Source: "..\dist\CehClient\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Цех"; Filename: "{app}\CehClient.exe"
Name: "{group}\Настройка подключения"; Filename: "{app}\CehClient.exe"; Parameters: "--configure"
Name: "{autodesktop}\Цех"; Filename: "{app}\CehClient.exe"
[Run]
Filename: "{app}\CehClient.exe"; Description: "Открыть Цех"; Flags: postinstall nowait skipifsilent
