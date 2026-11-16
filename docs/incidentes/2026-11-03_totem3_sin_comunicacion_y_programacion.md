# Incidente 2026-11-03 — Tótem #3 (Playin_Jockey) sin comunicación y sin programación

| Campo | Valor |
|---|---|
| Fecha | Martes 03/11/2026, ~10:40 – 20:15 (hora Lima) |
| Sede | Tienda Jockey Plaza |
| Equipos | PC central `DESKTOP-816CASQ` (servidor Docker, Wi-Fi) · Tótem #3 `Playin_Jockey` (Ethernet) |
| Síntoma reportado | "Falla en tótem: programación y comunicación" |
| Impacto | El tótem mostró solo `default.mp4` (pantalla de espera) durante el día; el contenido programado no se reproducía |
| Estado | **Resuelto** (comunicación y programación). Factores de red **en observación** |
| Responsable | Aldo |

---

## 1. Resumen

El tótem no reproducía la programación por **tres problemas independientes que coincidieron**:

1. **API Key distinta entre tótem y servidor** (causa de la falla de comunicación). El servidor
   usa una `API_KEY` propia definida en su `docker-compose.yml` de la tienda; el tótem seguía con
   el valor de ejemplo `hexa38-totem-key-change-me`. El servidor rechazaba latido, horario y
   descargas con `401`, y **el reproductor no registra los 401 en su log** (ver §4.1), por eso la
   falla fue silenciosa.
2. **Programación vacía** (causa de que no hubiera contenido). La tabla `programacion` estaba en
   cero. El tótem había sido **eliminado y vuelto a registrar** desde el panel; `eliminar_totem`
   borra en cascada su programación y sus logs, sin confirmación ni respaldo.
3. **Cortes intermitentes de red hacia la PC central** (`ConnectTimeoutError` cada 10–30 min).
   La central está por Wi-Fi con un adaptador USB Realtek 8811CU y tenía el perfil de red en
   *Público*. Causa probable, no confirmada todavía.

## 2. Línea de tiempo (hora Lima)

| Hora | Evento |
|---|---|
| 10:35 | La PC central enciende; el contenedor `hexa38_cms_server` arranca sin errores |
| 10:40 | El reproductor arranca; reproduce `default.mp4` (sin horario válido) |
| 10:54 – 17:36 | `reproductor.log`: `ConnectTimeoutError` intermitentes hacia `192.168.1.50:8080` en `/api/latido/3` y `/api/horario/3` |
| 17:47 | Técnico sale del modo kiosko (`Ctrl+Shift+Q` → "Salida de emergencia solicitada") |
| 17:50 | Inicio del diagnóstico. `ipconfig` del tótem: `192.168.1.23`, gateway `192.168.1.1` (Hitron) |
| 18:25 | `Test-NetConnection` desde el tótem: puerto 8080 **OK**, 5000 falla (esperado: Docker publica `8080→5000`) |
| 18:33 | `config_totem.json` del tótem apunta bien (`http://192.168.1.50:8080`) pero con la API Key de ejemplo |
| 18:35 | `docker exec … printenv API_KEY` en la central: la key real es otra → **discrepancia confirmada** |
| 18:43 | Se corrige `api_key` en `config_totem.json` |
| ~19:05 | Se reinicia `ReproductorTotem.exe` con la config nueva |
| 19:08 | El tótem recibe una **orden de apagado pendiente** en su primer latido válido (ver §4.4) |
| 19:11 | `wlanreport` de la central: sin desconexiones Wi-Fi hoy; adaptador USB Realtek 8811CU; Ethernet integrado sin cable |
| 19:20 | Perfil de red de la central cambiado de *Public* a *Private* |
| 19:39 | Dashboard: tótem #3 **Online** |
| 19:45 | Se desactiva la suspensión selectiva USB (`powercfg`) |
| 19:52 | `/api/horario/3` responde `200 []`: la programación está vacía |
| 19:55 | Base de datos: `programacion = 0`, `contenidos = 10`. Volúmenes Docker intactos |
| 20:00 | `sqlite_sequence` confirma ids no reutilizados: el tótem actual es el 3.er registro (ids 1 y 2 eliminados antes) |
| 20:04 | Programación recargada a mano; el tótem reproduce correctamente |
| 20:13 | Respaldo diario de la BD configurado (tarea programada 13:00) |
| 20:16 | IP del tótem corregida en el panel (`.33` → `.23`) |

## 3. Evidencia clave

- **Log Docker (central):** arranques diarios ~10:00–10:45, sin errores el 03/11. Sin access log, así
  que los `401` no aparecen en ningún lado.
- **`reproductor.log` (tótem):** solo `ConnectTimeoutError` / `Read timed out`; ningún `401`
  (porque el código no los registra).
- **`docker exec hexa38_cms_server env`:** `API_KEY` distinta a la del tótem.
- **`/api/horario/3` con la key correcta:** `200 []`.
- **SQLite:** `programacion = 0`, `contenidos = 10`, un solo juego de volúmenes
  (`servidor_tienda_nueva_cms_db`, `servidor_tienda_nueva_cms_media`).
- **`wlanreport`:** conexión Wi-Fi estable desde 10:35 (no hubo desasociación); el problema de red,
  si es del Wi-Fi, es de latencia o pérdida de paquetes, no de desconexión.

## 4. Hallazgos

### 4.1 El reproductor ignora en silencio las respuestas no-200 — **bug, alta**
En `reproductor.py`, si `/api/latido` o `/api/horario` devuelven `401` o `404`, no se escribe nada en
el log: el latido simplemente no actualiza el comando y el horario se trata como `sin_horario`
(reproduce `default.mp4`). Una API Key incorrecta o un tótem no registrado **se ven igual que "no hay
programación"**. Solo se registran las excepciones de red.

### 4.2 Eliminar un tótem borra su programación y sus logs sin confirmación — **riesgo, alta**
`eliminar_totem` ejecuta `DELETE FROM programacion WHERE totem_id = ?` y `DELETE FROM logs WHERE
totem_id = ?`. Al borrar también los logs, la sección *Actividad* no deja rastro del borrado. No había
respaldo de la base.

### 4.3 Servidor de la tienda desalineado con el repositorio — **media**
El `docker-compose.yml` de la tienda (carpeta `servidor_tienda_nueva` en la central) tiene
`API_KEY`/`SECRET_KEY` propias; el del repositorio sigue con los valores de ejemplo. Al instalar el
tótem se usó la key del repositorio. Además, el log muestra `WORKER TIMEOUT` con el timeout por
defecto (30 s), mientras el `dockerfile` del repositorio usa `--timeout 300`: la imagen de la tienda
podría ser anterior.

### 4.4 Comandos remotos quedan en cola indefinidamente — **media**
El comando `apagar` se guarda en `totems.comando` y se entrega en el siguiente latido **válido**,
sin caducidad. Una orden de apagado enviada horas antes (mientras los latidos fallaban) se ejecutó
a las 19:08, en cuanto se corrigió la key. *Pendiente:* revisar en *Actividad* la entrada "Comando
enviado: apagar" y confirmar quién la envió.

### 4.5 Red de la PC central — **media, en observación**
- Wi-Fi por dongle USB Realtek 8811CU; el Ethernet integrado no tiene cable (no es posible cablear).
- Perfil de red estaba en *Público* (corregido a *Privado*).
- IP `192.168.1.50` asignada por DHCP sin reserva (no hay acceso admin al router Hitron).

### 4.6 Hora mostrada en UTC — **baja**
El contenedor corre en UTC y el servidor guarda `datetime.now()`. El dashboard muestra el último latido
en UTC (00:39 del 04/11 cuando en Lima eran las 19:39 del 03/11).

### 4.7 Dato de IP desactualizado en el panel — **baja**
El tótem #3 figuraba con `192.168.1.33`; su IP real es `192.168.1.23`. El servidor no usa ese campo
para comunicarse (el tótem hace polling), así que no afectaba el funcionamiento.

## 5. Acciones aplicadas

- [x] `config_totem.json` del tótem actualizado con la `API_KEY` real del servidor.
- [x] Reproductor reiniciado con `-WorkingDirectory C:\Totem`.
- [x] Perfil de red Wi-Fi de la central → *Private*.
- [x] Suspensión selectiva USB desactivada (`powercfg … 48e6b7a6… 0`).
- [x] Programación del tótem #3 recargada desde el panel y verificada en pantalla.
- [x] Respaldo diario de `cms_totems.db`: `C:\backup_cms\backup.ps1` (API `backup()` de SQLite, conserva 14 días), tarea "Backup CMS Hexa38" a las 13:00.
- [x] IP del tótem corregida en el panel.

## 6. Pendientes

**Verificación (04/11):**
- [ ] Existe `C:\backup_cms\cms_totems_20261104.db` después de las 13:00.
- [ ] `C:\Totem\red.log` sin líneas `ping=False` / `tcp=False`.
- [ ] `Get-NetConnectionProfile` sigue en *Private* tras el reinicio diario.
- [ ] Desmarcar "Permitir que el equipo apague este dispositivo" en el adaptador Realtek 8811CU (no se aplicó: el parámetro de PowerShell no existe en esa versión).
- [ ] Rotar el código permanente de AweSun del tótem (quedó expuesto en capturas).
- [ ] Revisar en *Actividad* quién envió la orden de apagado.

**Infraestructura:**
- [ ] Evitar dependencia de la IP DHCP de la central: probar `url_servidor` por nombre (`DESKTOP-816CASQ.hitronhub.home` o `DESKTOP-816CASQ`) o conseguir acceso admin al router para reservar `.50` y `.23`.
- [ ] Mover el dongle Wi-Fi a un cable extensor USB con línea de vista al router; si persisten los timeouts, tarjeta Wi-Fi PCIe.

**Código** (resuelto en el repositorio el 15/11/2026, reproductor v2.0.0 — desplegado el 16/11/2026):
- [x] `reproductor.py`: registrar en log cualquier respuesta no-200 de latido/horario/descarga (código y cuerpo).
- [x] `servidor.py`: confirmación explícita al eliminar un tótem ("se borrará también su programación"); no borrar `logs` en cascada y registrar el borrado.
- [x] Comandos remotos con caducidad (`apagar` con más de 5 min se descarta y queda en *Actividad*).
- [x] Access log de gunicorn: `--access-logfile -` en el `CMD`.
- [x] `TZ=America/Lima` en el compose y `tzdata` en la imagen.
- [x] Keys vía `.env` (no en el compose versionado).
- [x] Reconstruir la imagen de la tienda con el código actual y reemplazar el `.exe` del tótem (desplegado 16/11/2026; servidor en `D:\Servidor_Tienda_Nueva`, respaldo previo en `respaldo_antes_v2_20261116_1432`).

## 7. Lecciones

- Que la red responda (`Test-NetConnection` OK) no prueba que la aplicación se comunique: hay que
  probar el endpoint con la key real.
- Un cliente que no registra los errores HTTP convierte una mala configuración en "no hay contenido".
- Sin access log en el servidor, los rechazos son invisibles en ambos extremos.
- Una acción destructiva sin confirmación ni respaldo convierte un clic en una reconstrucción manual.
