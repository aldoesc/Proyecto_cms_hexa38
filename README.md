# Hexa38 CMS — Sistema de Cartelería Digital (Tótems)

Sistema para gestionar pantallas/tótems de cartelería digital desde un panel web central.
Cada tótem es un equipo Windows que reproduce video/imágenes a pantalla completa según
un horario configurado en el servidor.

- **Servidor** (`servidor.py`): panel web Flask + base de datos SQLite. Corre en Docker.
- **Reproductor** (`reproductor.py` → `ReproductorTotem.exe`): app que corre en cada tótem.

---

## 1. Puesta en marcha del servidor

### Requisitos
- Docker y Docker Compose instalados en el equipo/servidor que hará de central.

### Pasos
1. Copia `.env.example` como `.env` (junto a `docker-compose.yml`) y pon **claves propias**:
   ```
   SECRET_KEY=<genera uno nuevo>
   API_KEY=<inventa una clave secreta>
   TZ=America/Lima
   ```
   Las claves viven en `.env` (no se versiona), así `docker-compose.yml` se puede
   actualizar desde el repositorio sin pisar las de cada tienda.
   Para generar un `SECRET_KEY` seguro:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
2. Levanta el servidor:
   ```bash
   docker compose up -d
   ```
3. Abre el panel en `http://<IP-del-servidor>:8080`
4. Inicia sesión con `admin` / `admin123` y **cambia la contraseña de inmediato**
   (menú → Cambiar contraseña).

El servidor tiene un chequeo de salud automático en `/api/health` (lo usa Docker para
reiniciar el contenedor si se cae).

---

## 2. Instalar un tótem nuevo (replicar en otra sede)

Este es el proceso para cada pantalla física:

### A. Registrar el tótem en el panel
1. Entra al panel web → **Equipos** → registra el equipo (nombre + IP).
2. Anota el **ID** que le asigna el sistema (ej. `2`).

### B. Preparar el equipo Windows del tótem
1. Copia a la PC del tótem la carpeta con:
   ```
   ReproductorTotem.exe
   vlc/                 ← carpeta VLC portable (debe ir junto al .exe)
   descargas_totem/     ← se crea sola; pon aquí default.mp4 si quieres pantalla de espera
   ```
2. Ejecuta `ReproductorTotem.exe`. La primera vez pide:
   - **ID del Totem**: el número del paso A (ej. `2`)
   - **URL del Servidor**: `http://<IP-del-servidor>:8080`
   - **API Key**: la misma `API_KEY` que pusiste en el `.env` del servidor
3. Guarda. Antes de entrar en kiosko el reproductor **prueba la conexión** y avisa si la
   URL o la API Key no son aceptadas por el servidor.

> La configuración se guarda en `config_totem.json` junto al .exe. Para reusar la misma
> config en varios tótems, copia ese archivo cambiando solo el `id_totem`.

### C. Arranque automático (recomendado para producción)
Para que el tótem arranque solo al encender el equipo, coloca un acceso directo a
`ReproductorTotem.exe` en la carpeta de inicio de Windows:
```
Win+R → shell:startup → pega un acceso directo al .exe
```

---

## 3. Operación diaria desde el panel

- **Subir contenido**: sube .mp4, .jpg o .png. En imágenes puedes fijar la **duración en
  pantalla** (segundos). Editable después desde la Biblioteca.
- **Programación**: asigna contenido a un tótem en un rango horario. Si dos horarios se
  solapan, el tótem reproduce ambos en bucle (el panel te avisa al guardar).
- **Dashboard**: estado en vivo de cada tótem (online/offline, almacenamiento, último latido),
  **qué está reproduciendo** y **cómo va su sincronización** (p. ej. `Descargando promo.mp4 45%
  · 2 de 6 archivo(s) pendientes`, o `Sin servidor: usando horario guardado`).
- **Apagar equipo**: envía orden de apagado físico al tótem. Si el tótem no la recoge en
  5 minutos se descarta (no se ejecuta horas después al volver la red).
- **Actividad**: historial de eventos (paginado).

---

## 4. Mantenimiento del tótem

- **Logs locales**: el reproductor escribe en `reproductor.log` (junto al .exe), con
  rotación automática (máx. ~6 MB). Útil para diagnosticar problemas sin monitor.
- **Salida del modo kiosko** (para un técnico): `Ctrl + Shift + Q`.
- **Pantalla de espera**: si no hay nada programado, reproduce `default.mp4` si existe en
  `descargas_totem/`.

### Cómo llega el contenido al tótem (v2)

- **Descarga verificada**: el servidor publica tamaño y SHA-256 de cada archivo; el tótem
  solo reproduce copias completas y correctas, y repara solo las dañadas.
- **Descarga reanudable**: si la red se corta, continúa desde donde quedó (`.part`) en vez de
  empezar de cero.
- **Descarga anticipada**: baja todo lo programado del día, no solo la franja actual; el
  cambio de franja no depende de la red en ese momento.
- **Horario en caché** (`horario_cache.json`): si el servidor no responde, el tótem sigue su
  programación —cambios de franja incluidos—, también tras reiniciarse sin red.
- **Latido independiente de las descargas**: un video grande ya no deja al tótem *Offline*.
- **Errores visibles**: un `401` (API Key) o `404` (tótem no registrado) queda en
  `reproductor.log` con su explicación, y en `docker compose logs` del servidor.
- **Limpieza**: lo que deja de estar programado se borra del tótem a los 7 días.
- **Autorrecuperación**: si VLC se queda parado o el reproductor falla, se relanza solo.

### Actualizar una instalación existente

Servidor y reproductor son compatibles entre versiones, se pueden actualizar por separado.
1. **Servidor**: copia el código nuevo, crea el `.env` con las claves **que ya usa esa tienda**
   (`docker exec hexa38_cms_server printenv API_KEY`) y ejecuta `docker compose up -d --build`.
   La base se migra sola al arrancar; haz antes un respaldo (ver docs/DIAGNOSTICO_TOTEM.md §5).
2. **Tótem**: recompila (§5) y reemplaza `ReproductorTotem.exe`. Conserva `config_totem.json`
   y `descargas_totem/`.

---

## 5. Recompilar el reproductor (.exe)

Solo si modificas `reproductor.py`:
```bash
pyinstaller ReproductorTotem.spec
```
El ejecutable queda en `dist/ReproductorTotem.exe`.

> **Dependencia VLC:** el reproductor usa VLC portable, que **no se incluye en el
> repositorio** (son ~185 MB de binarios de terceros). Descarga la versión portable
> de VLC para Windows desde [videolan.org](https://www.videolan.org/vlc/) y coloca
> la carpeta como `vlc_portable/` junto al proyecto (o `vlc/` junto al .exe en el tótem).

---

## 6. Diagnóstico e incidentes

- **Guía de diagnóstico paso a paso:** [docs/DIAGNOSTICO_TOTEM.md](docs/DIAGNOSTICO_TOTEM.md)
- **Registro de incidentes:** [docs/incidentes/](docs/incidentes/)

> Antes de eliminar un tótem desde *Equipos*: se borra también **toda su programación y sus logs**.
> Si solo quieres reinstalarlo, edítalo en lugar de eliminarlo.

---

## Checklist antes de producción

- [ ] `SECRET_KEY` propia en `.env`
- [ ] `API_KEY` propia en `.env`
- [ ] Contraseña de `admin` cambiada desde el panel
- [ ] `default.mp4` en `descargas_totem/` del tótem
- [ ] Reproductor en arranque automático de Windows
- [ ] Servidor accesible desde la red del tótem (`http://IP:8080`)
- [ ] `api_key` del tótem igual a la `API_KEY` del servidor (`docker exec hexa38_cms_server printenv API_KEY`)
- [ ] Perfil de red de la central en *Privado* (`Get-NetConnectionProfile`)
- [ ] IP de la central fija (reserva DHCP en el router)
- [ ] Respaldo diario de la base configurado (ver docs/DIAGNOSTICO_TOTEM.md §5)
