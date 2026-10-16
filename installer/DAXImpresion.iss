; DAX · Instalador del agente de impresión para Windows (Inno Setup 6).
; Lo compila la tarea de GitHub (.github/workflows/build.yml):  iscc /DAppVersion=1.2.0 installer\DAXImpresion.iss
; - Pide la dirección del POS y el código de 8 caracteres (Configuración → Impresión → Conectar un equipo).
; - La clave del equipo queda en C:\ProgramData\DAX, solo para SYSTEM y Administradores.
; - Deja una tarea que arranca con el equipo como SYSTEM (sin iniciar sesión) y se reabre si se cae.
; - Actualizar: se vuelve a ejecutar el instalador nuevo; conserva la conexión y no pide el código otra vez.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1D2C7A-3B84-4E5F-9A61-DA0C5E7B1A20}
AppName=DAX Impresión
AppVersion={#AppVersion}
AppVerName=DAX Impresión {#AppVersion}
AppPublisher=DAX
DefaultDirName={autopf}\DAX Impresion
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=DAX-Impresion-Instalar-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=DAX Impresión
UninstallDisplayIcon={app}\DAXPrintAgent.exe
CloseApplications=no

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[Files]
Source: "..\dist\DAXPrintAgent.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\DAX Impresión\Estado de la impresión"; Filename: "{app}\DAXPrintAgent.exe"; Parameters: "--home ""{commonappdata}\DAX"" --status --pause"
Name: "{autoprograms}\DAX Impresión\Ver registro de impresión"; Filename: "{sys}\notepad.exe"; Parameters: """{commonappdata}\DAX\print-agent.log"""

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Stop-ScheduledTask -TaskName 'DAX Print Agent' -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName 'DAX Print Agent' -Confirm:$false -ErrorAction SilentlyContinue"""; Flags: runhidden waituntilterminated; RunOnceId: "QuitarTarea"

[UninstallDelete]
; Desinstalar desconecta el equipo: se borra su clave. (Revócala también en el POS: Configuración → Integraciones.)
Type: filesandordirs; Name: "{commonappdata}\DAX"

[Code]
var
  ConnectPage: TInputQueryWizardPage;

function HomeDir: String;
begin
  Result := ExpandConstant('{commonappdata}\DAX');
end;

function AlreadyConnected: Boolean;
begin
  Result := FileExists(HomeDir + '\print-agent.json');
end;

procedure InitializeWizard;
begin
  ConnectPage := CreateInputQueryPage(wpWelcome,
    'Conectar con tu POS',
    'En el POS ve a Configuración → Impresión → «Conectar un equipo» y copia el código de 8 caracteres (vale 15 minutos).',
    '');
  ConnectPage.Add('Dirección del POS:', False);
  ConnectPage.Add('Código de 8 caracteres:', False);
  ConnectPage.Values[0] := 'https://www.theburgery.shop';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = ConnectPage.ID) and AlreadyConnected;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ConnectPage.ID then
  begin
    if Pos('http', Lowercase(Trim(ConnectPage.Values[0]))) <> 1 then
    begin
      MsgBox('Escribe la dirección completa del POS, por ejemplo https://www.theburgery.shop', mbError, MB_OK);
      Result := False;
    end
    else if Length(Trim(ConnectPage.Values[1])) <> 8 then
    begin
      MsgBox('El código tiene 8 caracteres. Genéralo en el POS: Configuración → Impresión → Conectar un equipo.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function SetupMessage: String;
var
  Lines: TArrayOfString;
begin
  { UTF-8 con BOM: LoadStringsFromFile respeta las tildes. }
  if LoadStringsFromFile(HomeDir + '\setup-result.txt', Lines) and (GetArrayLength(Lines) > 0) then
    Result := Lines[0]
  else
    Result := 'Sin detalle.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Exe, Home: String;
begin
  if CurStep <> ssPostInstall then
    exit;
  Exe := ExpandConstant('{app}\DAXPrintAgent.exe');
  Home := HomeDir;
  if not AlreadyConnected then
  begin
    WizardForm.StatusLabel.Caption := 'Conectando con el POS…';
    if (not Exec(Exe, '--home "' + Home + '" --setup --server "' + Trim(ConnectPage.Values[0]) + '" --code "' + Trim(ConnectPage.Values[1]) + '"',
                 '', SW_HIDE, ewWaitUntilTerminated, Code)) or (Code <> 0) then
    begin
      MsgBox('No se pudo conectar con el POS: ' + SetupMessage + #13#10#13#10 +
             'Genera un código nuevo en el POS y vuelve a ejecutar este instalador.', mbError, MB_OK);
      exit;
    end;
  end;
  WizardForm.StatusLabel.Caption := 'Dejando la impresión lista para arrancar con el equipo…';
  if (not Exec(Exe, '--home "' + Home + '" --install-system', '', SW_HIDE, ewWaitUntilTerminated, Code)) or (Code <> 0) then
    MsgBox('El equipo quedó conectado, pero no se pudo dejar arrancando solo. Abre «Estado de la impresión» en el menú Inicio para ver el detalle.', mbError, MB_OK);
end;
