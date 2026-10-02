; Instalador do Etiquetas Hortifruti (Inno Setup 6).
; Gere antes a pasta dist\EtiquetasHortifruti com compilar.bat.
;
; Instala por usuário em %LOCALAPPDATA%\Programs porque o banco (etiquetas.db),
; o config.json e o erro.log são gravados ao lado do .exe. Em Program Files o
; programa não teria permissão de escrita.

#define AppNome "Etiquetas Hortifruti"
#define AppExe "EtiquetasHortifruti.exe"
#define PastaDist "dist\EtiquetasHortifruti"
#define AppVersao GetVersionNumbersString(PastaDist + "\" + AppExe)

[Setup]
AppId={{8977FE25-0181-45A7-8A70-22321BAD47DF}
AppName={#AppNome}
AppVersion={#AppVersao}
AppVerName={#AppNome} {#AppVersao}
AppPublisher=Hortifruti Natural da Terra
VersionInfoVersion={#AppVersao}
DefaultDirName={localappdata}\Programs\EtiquetasHortifruti
DefaultGroupName={#AppNome}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.18363
OutputDir=dist\instalador
OutputBaseFilename=EtiquetasHortifruti-Setup-{#AppVersao}
SetupIconFile=logo.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppNome}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "atalhodesktop"; Description: "Criar atalho na área de trabalho"; GroupDescription: "Atalhos:"

[Files]
; Dados criados pelo programa ficam fora do pacote para que atualizar ou
; reinstalar nunca sobrescreva o banco nem as configurações da loja.
Source: "{#PastaDist}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; \
    Excludes: "etiquetas.db,etiquetas.db-wal,etiquetas.db-shm,etiquetas_dados.db*,config.json,erro.log,backups\*,logs\*,segredos\*,certificados\*"

[Icons]
Name: "{group}\{#AppNome}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\Desinstalar {#AppNome}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppNome}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: atalhodesktop

[Run]
Filename: "{app}\{#AppExe}"; Description: "Abrir {#AppNome}"; Flags: nowait postinstall skipifsilent
