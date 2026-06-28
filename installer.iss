; ============================================================
; Hexa38 Totem - Inno Setup Installer Script
; ============================================================
; Requisitos para compilar:
;   1. Inno Setup 6+ instalado (https://jrsoftware.org/isinfo.php)
;   2. ReproductorTotem.exe compilado en dist\
;   3. Carpeta "vlc\" con VLC portable (ver instrucciones abajo)
;
; Para obtener VLC portable:
;   - Descargar VLC portable (zip) de https://www.videolan.org/vlc/
;   - Extraer el contenido en una carpeta llamada "vlc" junto a este archivo
;   - Debe quedar: vlc\libvlc.dll, vlc\plugins\, etc.
;
; Compilar: abrir este archivo con Inno Setup y dar click en "Compile"
; ============================================================

[Setup]
AppName=Hexa38 Totem
AppVersion=1.0
AppPublisher=Hexa38
DefaultDirName=C:\Hexa38Totem
DefaultGroupName=Hexa38 Totem
UninstallDisplayIcon={app}\ReproductorTotem.exe
OutputDir=installer_output
OutputBaseFilename=Hexa38Totem_Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
DisableProgramGroupPage=yes
DisableDirPage=no

[Files]
; Ejecutable principal
Source: "dist\ReproductorTotem.exe"; DestDir: "{app}"; Flags: ignoreversion

; VLC portable embebido
Source: "vlc\*"; DestDir: "{app}\vlc"; Flags: ignoreversion recursesubdirs createallsubdirs

; Archivo por defecto (opcional, si existe)
Source: "default.mp4"; DestDir: "{app}\descargas_totem"; Flags: ignoreversion skipifsourcedoesntexist

[Dirs]
; Carpeta de descargas con permisos de escritura
Name: "{app}\descargas_totem"; Permissions: users-full

[Icons]
; Acceso directo en el escritorio
Name: "{commondesktop}\Hexa38 Totem"; Filename: "{app}\ReproductorTotem.exe"

; Acceso directo en el menu inicio
Name: "{group}\Hexa38 Totem"; Filename: "{app}\ReproductorTotem.exe"
Name: "{group}\Desinstalar Hexa38 Totem"; Filename: "{uninstallexe}"

[Registry]
; Inicio automatico con Windows
Root: HKLM; Subkey: "SOFTWARE\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "Hexa38Totem"; \
    ValueData: """{app}\ReproductorTotem.exe"""; Flags: uninsdeletevalue

[Run]
; Ejecutar al finalizar la instalacion (con permisos admin por el manifiesto)
Filename: "{app}\ReproductorTotem.exe"; \
    Description: "Iniciar Hexa38 Totem ahora"; \
    Flags: nowait postinstall skipifsilent runascurrentuser

[UninstallDelete]
; Limpiar archivos generados en runtime
Type: files; Name: "{app}\descargas_totem\*"
Type: dirifempty; Name: "{app}\descargas_totem"
Type: files; Name: "{app}\config_totem.json"

[Code]
// Cerrar el reproductor antes de desinstalar/actualizar
function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Exec('taskkill', '/F /IM ReproductorTotem.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;
