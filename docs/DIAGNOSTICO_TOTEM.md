# Guía de diagnóstico — Tótem sin contenido o sin comunicación

Orden recomendado: **red → API Key → registro del tótem → programación → descarga**.
Cada paso descarta una capa. No reinstales nada hasta saber en cuál falla.

> En los comandos, reemplaza `API_KEY_AQUI`, `IP_CENTRAL` e `ID` por los valores reales.
> No uses `<` `>` en PowerShell: los interpreta como redirección.

## 0. Datos que necesitas

| Dato | Dónde se obtiene |
|---|---|
| IP de la PC central | `ipconfig` en la central |
| Puerto publicado | `docker ps --format "table {{.Names}}\t{{.Ports}}"` (ej. `8080->5000`) |
| API Key del servidor | `docker exec hexa38_cms_server printenv API_KEY` |
| Config del tótem | `C:\Totem\config_totem.json` (`id_totem`, `url_servidor`, `api_key`) |
| Log del tótem | `C:\Totem\reproductor.log` |

## 1. Red (en el TÓTEM)

```powershell
Test-NetConnection IP_CENTRAL -Port 8080
```
- `PingSucceeded False`: la central está apagada, cambió de IP o no hay red.
- `PingSucceeded True` y `TcpTestSucceeded False`: firewall de Windows en la central (revisa que el perfil de red sea *Private*: `Get-NetConnectionProfile`) o Docker detenido.
- `TcpTestSucceeded True`: la red está bien → paso 2.

## 2. API Key

Compara `api_key` de `config_totem.json` con `printenv API_KEY` de la central. Deben ser idénticas.

> Desde el reproductor **v2.0.0** un `401` queda en `reproductor.log` ("el servidor RECHAZA la API
> Key") y en `docker compose logs` de la central ("401 API Key incorrecta ... desde IP").
> Con versiones anteriores el reproductor **no registra los 401**: una key incorrecta se ve igual
> que "no hay programación" (reproduce `default.mp4` y aparece *Offline* en el panel).
> La versión de cada tótem se ve en el Dashboard, bajo su nombre.

## 3. Registro del tótem y programación (en la CENTRAL)

```powershell
Invoke-WebRequest "http://localhost:8080/api/horario/ID?api_key=API_KEY_AQUI" -UseBasicParsing | Select-Object StatusCode, Content
```
- `401`: key incorrecta → paso 2.
- `200 []`: no hay programación para ese tótem → créala en el panel. Si existía antes, revisa si alguien eliminó el tótem (borra su programación en cascada) y restaura el respaldo.
- `200` con datos: el servidor entrega bien → paso 4.

Estado de la base:
```powershell
docker exec hexa38_cms_server python -c "import sqlite3;c=sqlite3.connect('/app/data/cms_totems.db');print('totems',c.execute('select id,nombre,ip,ultimo_latido from totems').fetchall());print('prog por totem',c.execute('select totem_id,count(*) from programacion group by totem_id').fetchall());print('contenidos',c.execute('select count(*) from contenidos').fetchall())"
```

## 4. Reproductor (en el TÓTEM)

```powershell
Select-String -Path C:\Totem\reproductor.log -Pattern "WARNING|ERROR" | Select-Object -Last 15
```
- `ConnectTimeoutError` intermitentes: problema de red de la central (Wi-Fi). Monitorea con:
  ```powershell
  while($true){$t=Get-Date -f "HH:mm:ss";$p=Test-Connection IP_CENTRAL -Count 1 -Quiet;$c=(Test-NetConnection IP_CENTRAL -Port 8080 -WarningAction SilentlyContinue).TcpTestSucceeded;"$t ping=$p tcp=$c" | Tee-Object -Append C:\Totem\red.log;Start-Sleep 10}
  ```
  `ping=False` → radio/Wi-Fi. `ping=True` y `tcp=False` → Docker o firewall.
- `Read timed out` en descargas de video: workers de gunicorn ocupados.

Reiniciar el reproductor (toma la config nueva):
```powershell
Stop-Process -Name ReproductorTotem -Force -ErrorAction SilentlyContinue
Start-Process C:\Totem\ReproductorTotem.exe -WorkingDirectory C:\Totem
```
Sin `-WorkingDirectory` no encuentra `config_totem.json`. Salir del kiosko: `Ctrl+Shift+Q`.

> El reproductor es un `.exe` (PyInstaller). En el tótem **no hay Python instalado**: `python archivo.py` no funciona allí.

## 5. Respaldo y restauración de la base

Respaldo (automático, diario 13:00, últimos 14 días): `C:\backup_cms\backup.ps1`.

Restaurar un respaldo:
```powershell
docker cp C:\backup_cms\cms_totems_AAAAMMDD.db hexa38_cms_server:/app/data/cms_totems.db
docker restart hexa38_cms_server
```

## Historial de incidentes

- [2026-11-03 — Tótem #3 sin comunicación y sin programación](incidentes/2026-11-03_totem3_sin_comunicacion_y_programacion.md)
