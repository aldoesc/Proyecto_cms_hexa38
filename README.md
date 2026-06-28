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
1. Edita `docker-compose.yml` y **cambia los dos valores por defecto**:
   ```yaml
   - SECRET_KEY=<genera uno nuevo>
   - API_KEY=<inventa una clave secreta>
   ```
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
   - **API Key**: la misma `API_KEY` que pusiste en `docker-compose.yml`
3. Guarda. El reproductor entra en modo kiosko (pantalla completa) y queda operativo.

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
- **Dashboard**: estado en vivo de cada tótem (online/offline, almacenamiento, último latido).
- **Apagar equipo**: envía orden de apagado físico al tótem.
- **Actividad**: historial de eventos (paginado).

---

## 4. Mantenimiento del tótem

- **Logs locales**: el reproductor escribe en `reproductor.log` (junto al .exe), con
  rotación automática (máx. ~6 MB). Útil para diagnosticar problemas sin monitor.
- **Salida del modo kiosko** (para un técnico): `Ctrl + Shift + Q`.
- **Pantalla de espera**: si no hay nada programado, reproduce `default.mp4` si existe en
  `descargas_totem/`.

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

## Checklist antes de producción

- [ ] `SECRET_KEY` cambiada en `docker-compose.yml`
- [ ] `API_KEY` cambiada en `docker-compose.yml`
- [ ] Contraseña de `admin` cambiada desde el panel
- [ ] `default.mp4` en `descargas_totem/` del tótem
- [ ] Reproductor en arranque automático de Windows
- [ ] Servidor accesible desde la red del tótem (`http://IP:8080`)
