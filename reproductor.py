import time
import requests
import os
import json
import sys
import shutil
import hashlib
import logging
import queue
import threading
import subprocess
from logging.handlers import RotatingFileHandler
from datetime import datetime
import tkinter as tk
from tkinter import messagebox

VERSION = "2.0.0"

# En modo --onefile, __file__ apunta a una carpeta temporal.
# Usar sys.executable para obtener la ruta real del .exe
if getattr(sys, 'frozen', False):
    directorio_actual = os.path.dirname(sys.executable)
else:
    directorio_actual = os.path.dirname(os.path.abspath(__file__))

# ==========================================
# CONFIGURACIÓN DE LOGGING (consola + archivo rotativo)
# ==========================================
# El tótem no tiene teclado/monitor accesible; sin log a disco los errores se
# pierden. RotatingFileHandler evita que el archivo crezca indefinidamente.
log = logging.getLogger("totem")
log.setLevel(logging.INFO)
_formato = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

_handler_consola = logging.StreamHandler()
_handler_consola.setFormatter(_formato)
log.addHandler(_handler_consola)

try:
    _archivo_log = os.path.join(directorio_actual, "reproductor.log")
    _handler_archivo = RotatingFileHandler(_archivo_log, maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8')
    _handler_archivo.setFormatter(_formato)
    log.addHandler(_handler_archivo)
except Exception as e:
    log.warning(f"No se pudo crear el archivo de log: {e}")

# Localizar VLC portable. Orden de búsqueda:
#  1. VLC empaquetado dentro del .exe (PyInstaller lo extrae a sys._MEIPASS)
#  2. Carpeta vlc/ junto al ejecutable (modo portable manual)
# Así NO hace falta instalar VLC en el tótem.
posibles_rutas_vlc = []
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    # OJO: la carpeta empaquetada NO puede llamarse "vlc" porque colisiona con
    # el módulo python-vlc (import vlc encontraría la carpeta en vez del módulo).
    posibles_rutas_vlc.append(os.path.join(sys._MEIPASS, "vlcbin"))
posibles_rutas_vlc.append(os.path.join(directorio_actual, "vlc_portable"))
posibles_rutas_vlc.append(os.path.join(directorio_actual, "vlc"))

for vlc_portable in posibles_rutas_vlc:
    if os.path.isdir(vlc_portable):
        os.environ['PYTHON_VLC_MODULE_PATH'] = vlc_portable
        os.environ['PYTHON_VLC_LIB_PATH'] = os.path.join(vlc_portable, "libvlc.dll")
        # VLC necesita saber dónde están sus plugins para decodificar video
        os.environ['VLC_PLUGIN_PATH'] = os.path.join(vlc_portable, "plugins")
        if vlc_portable not in os.environ.get('PATH', ''):
            os.environ['PATH'] = vlc_portable + os.pathsep + os.environ.get('PATH', '')
        break

import vlc
CARPETA_LOCAL = os.path.join(directorio_actual, "descargas_totem")
ARCHIVO_CONFIG = os.path.join(directorio_actual, "config_totem.json")
# Último horario recibido del servidor. Permite seguir la programación (cambios de
# franja incluidos) con el servidor caído o tras reiniciar el tótem sin red.
ARCHIVO_CACHE_HORARIO = os.path.join(directorio_actual, "horario_cache.json")
ARCHIVO_HUERFANOS = os.path.join(directorio_actual, "huerfanos.json")
ARCHIVO_POR_DEFECTO = "default.mp4"
INTERVALO_POLLING_MS = 10000  # 10 segundos
INTERVALO_VIGILANCIA_MS = 5000
API_KEY_POR_DEFECTO = "hexa38-totem-key-change-me"

TIMEOUT_API = (5, 10)        # (conexión, lectura) para latido/horario
TIMEOUT_DESCARGA = (5, 60)   # lectura: máximo sin recibir ni un byte
MARGEN_DISCO_BYTES = 200 * 1024 * 1024   # espacio libre mínimo tras una descarga
DIAS_GRACIA_HUERFANOS = 7    # un archivo ya no programado se conserva estos días
REINTENTO_MAX_SEG = 120
EXT_IMAGEN = ('.jpg', '.jpeg', '.png', '.bmp', '.gif')

if not os.path.exists(CARPETA_LOCAL):
    os.makedirs(CARPETA_LOCAL)


# ==========================================
# AVISOS SIN INUNDAR EL LOG
# ==========================================
# Un fallo persistente (servidor caído, API Key mala) se repite cada 10 s. Se
# registra la primera vez y luego cada ~5 min, y se anota cuándo se recupera.
_fallos_seguidos = {}


def _aviso(clave, mensaje, nivel=logging.WARNING):
    n = _fallos_seguidos.get(clave, 0) + 1
    _fallos_seguidos[clave] = n
    if n == 1:
        log.log(nivel, mensaje)
    elif n % 30 == 0:
        log.log(nivel, f"{mensaje} (persiste, {n} intentos seguidos)")


def _aviso_ok(clave):
    n = _fallos_seguidos.pop(clave, 0)
    if n:
        log.info(f"Recuperado: {clave} vuelve a funcionar tras {n} fallo(s)")


def _describir_http(respuesta):
    """Explica una respuesta no-200 del servidor en términos accionables."""
    codigo = respuesta.status_code
    if codigo == 401:
        return ("401 - el servidor RECHAZA la API Key. Revisa 'api_key' en config_totem.json; "
                "debe ser idéntica a la API_KEY del servidor")
    if codigo == 404:
        return "404 - este ID de tótem no está registrado en el servidor (o el recurso no existe)"
    try:
        cuerpo = respuesta.text[:200].replace('\n', ' ')
    except Exception:
        cuerpo = ''
    return f"HTTP {codigo} {cuerpo}".strip()


def _leer_json(ruta, por_defecto):
    try:
        with open(ruta, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return por_defecto


def _escribir_json(ruta, datos):
    """Escritura atómica: un corte de luz no deja el archivo a medias."""
    tmp = ruta + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(datos, f)
        os.replace(tmp, ruta)
    except OSError as e:
        log.warning(f"No se pudo escribir {os.path.basename(ruta)}: {e}")


# ==========================================
# INTEGRIDAD DE ARCHIVOS
# ==========================================
def _sha256_archivo(ruta):
    h = hashlib.sha256()
    with open(ruta, 'rb') as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b''):
            h.update(bloque)
    return h.hexdigest()


# nombre -> (tamaño, mtime, sha256) ya comprobado: el hash se calcula una sola vez
# por archivo y ejecución; después basta un stat().
_verificados = {}


def _nombre_seguro(nombre):
    return bool(nombre) and os.path.basename(nombre) == nombre and nombre not in ('.', '..')


def archivo_valido(nombre, tamano=None, sha256=None):
    """True si el archivo local existe y coincide con lo que publica el servidor.
    Si el servidor no informa tamaño/hash (versión antigua) basta con que no esté vacío."""
    ruta = os.path.join(CARPETA_LOCAL, nombre)
    try:
        st = os.stat(ruta)
    except OSError:
        return False
    if st.st_size == 0:
        return False
    if tamano and st.st_size != tamano:
        return False
    if sha256:
        clave = (st.st_size, st.st_mtime_ns, sha256)
        if _verificados.get(nombre) != clave:
            try:
                if _sha256_archivo(ruta) != sha256:
                    return False
            except OSError:
                return False
            _verificados[nombre] = clave
    return True


# ==========================================
# DESCARGA REANUDABLE Y VERIFICADA
# ==========================================
# Progreso de la descarga en curso (lo lee el latido para informarlo al panel).
progreso_descarga = {}


def descargar_archivo(nombre_archivo, url_completa, api_key, tamano=None, sha256=None, sesion=None):
    """Descarga a '<nombre>.part' y solo al terminar y verificar (tamaño + SHA-256)
    reemplaza el archivo final. Si la conexión se corta, el .part se conserva y el
    siguiente intento continúa desde donde quedó (HTTP Range) en vez de empezar de cero.
    Devuelve la ruta final o None."""
    if not _nombre_seguro(nombre_archivo):
        log.error(f"Nombre de archivo no válido recibido del servidor: {nombre_archivo!r}")
        return None
    ruta_final = os.path.join(CARPETA_LOCAL, nombre_archivo)
    ruta_part = ruta_final + ".part"
    if archivo_valido(nombre_archivo, tamano, sha256):
        return ruta_final
    if os.path.exists(ruta_final):
        log.warning(f"{nombre_archivo}: la copia local no coincide con el servidor "
                    f"(truncada, corrupta o reemplazada); se vuelve a descargar")

    sesion = sesion or requests
    esperado = tamano or None
    inicio = os.path.getsize(ruta_part) if os.path.exists(ruta_part) else 0
    if esperado and inicio > esperado:
        os.remove(ruta_part)
        inicio = 0

    if not (esperado and inicio == esperado):
        if esperado:
            libre = shutil.disk_usage(CARPETA_LOCAL).free
            if libre < (esperado - inicio) + MARGEN_DISCO_BYTES:
                log.error(f"Sin espacio en disco para {nombre_archivo}: faltan "
                          f"{(esperado - inicio) // (1024 * 1024)} MB y quedan {libre // (1024 * 1024)} MB libres")
                return None
        headers = {'X-API-Key': api_key}
        if inicio:
            headers['Range'] = f"bytes={inicio}-"
            log.info(f"Reanudando descarga de {nombre_archivo} desde {inicio // 1024} KB")
        else:
            log.info(f"Descargando: {nombre_archivo}...")
        try:
            with sesion.get(url_completa, stream=True, timeout=TIMEOUT_DESCARGA, headers=headers) as resp:
                if resp.status_code == 416:
                    # El parcial ya no corresponde al archivo del servidor: empezar de cero.
                    log.warning(f"{nombre_archivo}: descarga parcial obsoleta, se reinicia")
                    os.remove(ruta_part)
                    return None
                if resp.status_code == 206 and inicio:
                    modo = 'ab'
                    total = resp.headers.get('Content-Range', '').rpartition('/')[2]
                    if not esperado and total.isdigit():
                        esperado = int(total)
                elif resp.status_code == 200:
                    modo = 'wb'   # el servidor ignoró el Range: archivo completo
                    inicio = 0
                    largo = resp.headers.get('Content-Length', '')
                    if not esperado and largo.isdigit():
                        esperado = int(largo)
                else:
                    log.error(f"No se pudo descargar {nombre_archivo}: {_describir_http(resp)}")
                    return None
                recibido = inicio
                progreso_descarga.update(nombre=nombre_archivo, recibido=recibido, total=esperado or 0)
                with open(ruta_part, modo) as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            recibido += len(chunk)
                            progreso_descarga['recibido'] = recibido
        except (requests.exceptions.RequestException, OSError) as e:
            parcial = os.path.getsize(ruta_part) if os.path.exists(ruta_part) else 0
            log.error(f"Descarga de {nombre_archivo} interrumpida a los {parcial // 1024} KB "
                      f"(se reanudará): {e}")
            return None
        finally:
            progreso_descarga.clear()

    real = os.path.getsize(ruta_part) if os.path.exists(ruta_part) else 0
    if real == 0 or (esperado and real != esperado):
        log.error(f"Descarga incompleta de {nombre_archivo}: {real} de {esperado or '?'} bytes")
        if esperado and real > esperado:
            os.remove(ruta_part)
        return None
    if sha256:
        if _sha256_archivo(ruta_part) != sha256:
            log.error(f"{nombre_archivo}: el contenido descargado no coincide con el SHA-256 "
                      f"del servidor; se descarta y se reintentará")
            os.remove(ruta_part)
            return None
    try:
        os.replace(ruta_part, ruta_final)
    except OSError as e:
        # En Windows ocurre si VLC tiene abierto el archivo antiguo. El .part ya está
        # completo: el próximo intento solo repite el reemplazo, no la descarga.
        log.warning(f"{nombre_archivo} descargado, pero no se pudo reemplazar la copia en uso "
                    f"(se reintentará): {e}")
        return None
    log.info(f"Descarga completa y verificada: {nombre_archivo} ({real // 1024} KB)")
    return ruta_final


# ==========================================
# HORARIO (servidor con respaldo en caché local)
# ==========================================
def _tarea_valida(tarea):
    return (isinstance(tarea, dict)
            and _nombre_seguro(tarea.get('nombre_archivo'))
            and isinstance(tarea.get('hora_inicio'), str)
            and isinstance(tarea.get('hora_fin'), str)
            and isinstance(tarea.get('url_descarga'), str))


def obtener_horario(url_servidor, id_totem, headers, sesion=None):
    """Devuelve (horario, origen, error). origen es 'servidor', 'cache' o None.
    Si el servidor no responde bien se usa el último horario guardado en disco."""
    sesion = sesion or requests
    error = None
    try:
        resp = sesion.get(f"{url_servidor}/api/horario/{id_totem}", headers=headers, timeout=TIMEOUT_API)
        if resp.status_code == 200:
            datos = resp.json()
            if isinstance(datos, list):
                horario = [t for t in datos if _tarea_valida(t)]
                if len(horario) != len(datos):
                    log.warning(f"Horario: {len(datos) - len(horario)} entrada(s) con formato inválido ignoradas")
                _aviso_ok('horario')
                previo = _leer_json(ARCHIVO_CACHE_HORARIO, {})
                if previo.get('horario') != horario or previo.get('id_totem') != id_totem:
                    _escribir_json(ARCHIVO_CACHE_HORARIO, {
                        'id_totem': id_totem,
                        'guardado': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        'horario': horario,
                    })
                return horario, 'servidor', None
            error = "respuesta de horario con formato inesperado"
        else:
            error = _describir_http(resp)
    except requests.exceptions.RequestException as e:
        error = f"sin conexión con el servidor ({e.__class__.__name__}: {e})"
    except ValueError:
        error = "respuesta de horario que no es JSON válido"

    cache = _leer_json(ARCHIVO_CACHE_HORARIO, None)
    if (isinstance(cache, dict) and cache.get('id_totem') == id_totem
            and isinstance(cache.get('horario'), list)):
        _aviso('horario', f"No se pudo obtener el horario: {error}. "
                          f"Se usa el horario guardado el {cache.get('guardado', '?')}")
        return [t for t in cache['horario'] if _tarea_valida(t)], 'cache', error
    _aviso('horario', f"No se pudo obtener el horario: {error}. No hay horario guardado en disco")
    return None, None, error


def tareas_vigentes(horario, ahora=None):
    ahora = ahora or datetime.now().strftime("%H:%M")
    return [t for t in horario if t['hora_inicio'] <= ahora <= t['hora_fin']]


def limpiar_huerfanos(horario):
    """Borra del disco lo que ya no está programado, con un periodo de gracia: si la
    programación se borra por error en el panel, el contenido sigue en el tótem unos
    días y no hay que volver a bajarlo. Solo se llama con un horario fresco del servidor."""
    referenciados = {t['nombre_archivo'] for t in horario} | {ARCHIVO_POR_DEFECTO}
    estado = _leer_json(ARCHIVO_HUERFANOS, {})
    if not isinstance(estado, dict):
        estado = {}
    ahora = time.time()
    nuevo = {}
    try:
        nombres = os.listdir(CARPETA_LOCAL)
    except OSError:
        return
    for nombre in nombres:
        ruta = os.path.join(CARPETA_LOCAL, nombre)
        if not os.path.isfile(ruta):
            continue
        parcial = nombre.endswith(('.part', '.tmp'))
        base = nombre.rsplit('.', 1)[0] if parcial else nombre
        if base in referenciados and not nombre.endswith('.tmp'):
            continue
        desde = estado.get(nombre, ahora)
        if parcial or ahora - desde > DIAS_GRACIA_HUERFANOS * 86400:
            try:
                os.remove(ruta)
                log.info(f"Limpieza: eliminado {nombre} (ya no está programado)")
            except OSError:
                nuevo[nombre] = desde
        else:
            nuevo[nombre] = desde
    if nuevo != estado:
        _escribir_json(ARCHIVO_HUERFANOS, nuevo)


# ==========================================
# DESCARGADOR EN SEGUNDO PLANO
# ==========================================
class Descargador:
    """Hilo único que baja los archivos pendientes de uno en uno. Va separado del
    ciclo de latido para que una descarga larga no deje al tótem 'Offline' en el
    panel ni retrase los cambios de franja horaria."""

    def __init__(self, url_servidor, api_key):
        self.url_servidor = url_servidor
        self.api_key = api_key
        self._candado = threading.Lock()
        self._deseados = []      # tareas en orden de prioridad (lo vigente primero)
        self._reintentos = {}    # nombre -> (fallos, no_antes_de)
        self._despertar = threading.Event()
        self._sesion = requests.Session()
        threading.Thread(target=self._bucle, daemon=True, name="descargas").start()

    def fijar_deseados(self, tareas):
        with self._candado:
            self._deseados = list(tareas)
        self._despertar.set()

    def _siguiente(self):
        with self._candado:
            deseados = list(self._deseados)
        ahora = time.time()
        vistos = set()
        for t in deseados:
            nombre = t['nombre_archivo']
            if nombre in vistos:
                continue
            vistos.add(nombre)
            if ahora < self._reintentos.get(nombre, (0, 0))[1]:
                continue
            if not archivo_valido(nombre, t.get('tamano'), t.get('sha256')):
                return t
        return None

    def _bucle(self):
        while True:
            try:
                tarea = self._siguiente()
                if tarea is None:
                    self._despertar.wait(5)
                    self._despertar.clear()
                    continue
                nombre = tarea['nombre_archivo']
                ruta_part = os.path.join(CARPETA_LOCAL, nombre + ".part")
                antes = os.path.getsize(ruta_part) if os.path.exists(ruta_part) else 0
                ruta = descargar_archivo(nombre, f"{self.url_servidor}{tarea['url_descarga']}",
                                         self.api_key, tarea.get('tamano'), tarea.get('sha256'),
                                         sesion=self._sesion)
                if ruta:
                    self._reintentos.pop(nombre, None)
                    continue
                despues = os.path.getsize(ruta_part) if os.path.exists(ruta_part) else 0
                # Si avanzó algo se reintenta enseguida (reanuda); si no, espera creciente
                # para no martillar al servidor con un archivo que falla siempre.
                fallos = 0 if despues > antes else self._reintentos.get(nombre, (0, 0))[0] + 1
                espera = min(5 * (2 ** fallos), REINTENTO_MAX_SEG)
                self._reintentos[nombre] = (fallos, time.time() + espera)
            except Exception:
                log.exception("Error inesperado en el hilo de descargas")
                time.sleep(10)


def resumen_sincronizacion(horario, origen):
    """Texto corto para el panel: qué le falta al tótem por descargar."""
    if horario is None:
        return "Sin conexión y sin horario guardado"
    nombres = {}
    for t in horario:
        nombres.setdefault(t['nombre_archivo'], t)
    faltan = [n for n, t in nombres.items() if not archivo_valido(n, t.get('tamano'), t.get('sha256'))]
    partes = []
    if origen == 'cache':
        partes.append("Sin servidor: usando horario guardado")
    if faltan:
        p = dict(progreso_descarga)
        if p.get('nombre'):
            pct = f" {int(p['recibido'] * 100 / p['total'])}%" if p.get('total') else ""
            partes.append(f"Descargando {p['nombre']}{pct}")
        partes.append(f"{len(faltan)} de {len(nombres)} archivo(s) pendientes")
    elif origen == 'servidor':
        partes.append(f"Sincronizado ({len(nombres)} archivo(s))")
    return " · ".join(partes)


# ==========================================
# REPRODUCTOR KIOSKO CON PANTALLA COMPLETA
# ==========================================
_descargador = None


def iniciar_bucle_totem(id_totem, url_servidor, api_key=API_KEY_POR_DEFECTO):
    global _descargador
    log.info(f"Iniciando Totem ID: {id_totem} (reproductor v{VERSION})")
    log.info(f"Servidor: {url_servidor}")
    if api_key == API_KEY_POR_DEFECTO:
        log.warning("Este tótem usa la API Key de ejemplo. Si el servidor tiene otra, "
                    "rechazará todas las peticiones (401).")
    headers = {'X-API-Key': api_key}
    sesion_api = requests.Session()   # reutiliza la conexión: menos coste por ciclo en wifi lento
    if _descargador is None:
        _descargador = Descargador(url_servidor, api_key)
    descargador = _descargador

    # --- Ventana fullscreen con tkinter ---
    root = tk.Tk()
    root.title("Totem")
    root.configure(background='black')
    root.attributes('-fullscreen', True)
    root.attributes('-topmost', True)
    root.config(cursor="none")
    # Bloquear cierre con Alt+F4
    root.protocol("WM_DELETE_WINDOW", lambda: None)
    # Bloquear Escape
    root.bind("<Escape>", lambda e: None)
    # Salida de emergencia: Ctrl+Shift+Q (variantes para compatibilidad Windows)
    root.bind("<Control-Shift-Q>", lambda e: _salida_emergencia(root, reproductor_lista))
    root.bind("<Control-Shift-q>", lambda e: _salida_emergencia(root, reproductor_lista))
    root.bind("<Control-Q>", lambda e: _salida_emergencia(root, reproductor_lista))
    # Un error dentro de un callback de Tk no debe pasar desapercibido.
    root.report_callback_exception = lambda *exc: log.error("Error en callback de Tk", exc_info=exc)

    # Frame donde VLC renderiza el video
    video_frame = tk.Frame(root, bg='black')
    video_frame.pack(fill=tk.BOTH, expand=True)

    # Forzar que el frame tenga un handle de ventana valido
    root.update_idletasks()
    root.update()

    # --- VLC embebido en la ventana tkinter ---
    vlc_args = [
        "--no-video-title-show",
        "--mouse-hide-timeout=0",
        "--no-osd",
        "--no-snapshot-preview",
        "--aout=directsound" if sys.platform.startswith('win') else "--aout=pulse",
    ]
    instancia_vlc = vlc.Instance(*vlc_args)
    reproductor_lista = instancia_vlc.media_list_player_new()
    reproductor_individual = instancia_vlc.media_player_new()
    reproductor_lista.set_media_player(reproductor_individual)
    reproductor_lista.set_playback_mode(vlc.PlaybackMode.loop)

    # Vincular VLC al frame de tkinter (clave para fullscreen real)
    handle = video_frame.winfo_id()
    if sys.platform.startswith('win'):
        reproductor_individual.set_hwnd(handle)
    else:
        reproductor_individual.set_xwindow(handle)

    # --- Estado mutable del ciclo ---
    # Firma de lo que suena: tupla de (nombre, duración, sha). Cambia si cambia el
    # contenido, el orden, la duración de una imagen o el archivo en sí.
    firma_actual = None
    FIRMA_DEFAULT = (('__default__', 0, None),)
    # Lo que se informa al servidor en cada latido (lo escribe el hilo principal).
    estado_reporte = {'reproduciendo': '', 'sync': ''}
    paradas_seguidas = 0
    # Cola para pasar resultados del hilo de red al hilo principal de Tkinter.
    # tkinter NO es thread-safe: TODA operación de UI/VLC ocurre en el hilo principal.
    cola_resultados = queue.Queue()
    # Evita solapar ciclos de red si el servidor tarda en responder.
    trabajo_activo = threading.Event()

    def _reproducir_default(motivo=""):
        """Reproduce default.mp4 (si existe localmente) para que el tótem opere de
        forma autónoma sin depender del servidor. No reinicia si el default ya está
        sonando. Devuelve True si el default está disponible en disco."""
        nonlocal firma_actual
        ruta_defecto = os.path.join(CARPETA_LOCAL, ARCHIVO_POR_DEFECTO)
        if not os.path.exists(ruta_defecto):
            return False
        if firma_actual != FIRMA_DEFAULT:
            log.info(f"Reproduciendo default.mp4{(' (' + motivo + ')') if motivo else ''}")
            lista_vlc = instancia_vlc.media_list_new([ruta_defecto])
            reproductor_lista.set_media_list(lista_vlc)
            reproductor_lista.play()
            firma_actual = FIRMA_DEFAULT
            estado_reporte['reproduciendo'] = ARCHIVO_POR_DEFECTO + " (espera)"
        return True

    def _trabajo_red():
        """Envoltura del hilo de red: libera el candado de ciclo al terminar,
        incluso si ocurre un error inesperado."""
        try:
            _trabajo_red_impl()
        except Exception:
            log.exception("Error inesperado en el ciclo de red")
        finally:
            trabajo_activo.clear()

    def _trabajo_red_impl():
        """Se ejecuta en un hilo aparte. Solo hace red (rápida: latido + horario) y
        comprueba archivos locales; nunca toca Tkinter ni VLC ni descarga (eso lo hace
        el Descargador). Deja el resultado en la cola."""
        # 1. LATIDO (incluye el estado real del tótem para verlo en el panel)
        comando = 'ninguno'
        try:
            disco = shutil.disk_usage(os.path.abspath(os.sep))
            porcentaje_uso = int((disco.used / disco.total) * 100)
            resp = sesion_api.post(
                f"{url_servidor}/api/latido/{id_totem}",
                json={
                    "almacenamiento": porcentaje_uso,
                    "version": VERSION,
                    "reproduciendo": estado_reporte['reproduciendo'],
                    "sync": estado_reporte['sync'],
                },
                headers=headers,
                timeout=TIMEOUT_API
            )
            if resp.status_code == 200:
                _aviso_ok('latido')
                comando = resp.json().get("comando", "ninguno")
            else:
                _aviso('latido', f"Latido rechazado por el servidor: {_describir_http(resp)}", logging.ERROR)
        except requests.exceptions.RequestException as e:
            _aviso('latido', f"Error en latido: {e}")
        except ValueError:
            _aviso('latido', "Latido: respuesta del servidor que no es JSON válido")

        # 2. APAGADO REMOTO
        if comando == "apagar":
            cola_resultados.put({'tipo': 'apagar'})
            return

        # 3. HORARIO (del servidor o, si falla, el último guardado en disco)
        horario, origen, _error = obtener_horario(url_servidor, id_totem, headers, sesion=sesion_api)
        if horario is None:
            estado_reporte['sync'] = resumen_sincronizacion(None, None)
            cola_resultados.put({'tipo': 'sin_conexion'})
            return

        vigentes = tareas_vigentes(horario)
        if origen == 'servidor':
            # Lo vigente primero; el resto del día se baja por adelantado para que el
            # cambio de franja no dependa de la red en ese momento.
            descargador.fijar_deseados(vigentes + [t for t in horario if t not in vigentes])
        else:
            descargador.fijar_deseados([])

        # 4. PLAYLIST con lo que ya está completo y verificado en disco
        if vigentes:
            items = []
            for tarea in vigentes:
                nombre = tarea['nombre_archivo']
                if archivo_valido(nombre, tarea.get('tamano'), tarea.get('sha256')):
                    try:
                        duracion = int(tarea.get('duracion') or 10)
                    except (TypeError, ValueError):
                        duracion = 10
                    items.append({
                        'nombre': nombre,
                        'ruta': os.path.join(CARPETA_LOCAL, nombre),
                        'duracion': duracion,
                        'sha256': tarea.get('sha256'),
                    })
            cola_resultados.put({'tipo': 'playlist', 'items': items})
        else:
            cola_resultados.put({'tipo': 'sin_horario'})

        estado_reporte['sync'] = resumen_sincronizacion(horario, origen)
        if origen == 'servidor':
            limpiar_huerfanos(horario)

    def _aplicar_resultado(resultado):
        """Se ejecuta en el hilo principal. Aquí sí se puede tocar VLC/Tkinter."""
        nonlocal firma_actual
        tipo = resultado.get('tipo')

        if tipo == 'apagar':
            log.warning("ORDEN DE APAGADO RECIBIDA")
            reproductor_lista.stop()
            # quit() sale del mainloop limpiamente; destroy() aquí daría TclError
            root.quit()
            try:
                if sys.platform.startswith('win'):
                    subprocess.run(["shutdown", "/s", "/t", "5"], shell=True)
                else:
                    subprocess.run(["shutdown", "-h", "+0"], shell=True)
            except Exception as e:
                log.error(f"Error ejecutando apagado: {e}")
            return

        if tipo == 'playlist':
            items = resultado['items']
            # Solo los archivos YA descargados forman la lista; así no se reinicia en
            # cada ciclo mientras un video grande todavía se está descargando.
            firma = tuple((it['nombre'], it['duracion'], it['sha256']) for it in items)
            if items and firma != firma_actual:
                disponibles = [it['nombre'] for it in items]
                log.info(f"Nueva playlist: {disponibles}")
                lista_vlc = instancia_vlc.media_list_new()
                for it in items:
                    media = instancia_vlc.media_new(it['ruta'])
                    # Las imágenes no tienen duración propia; VLC las pasaría en ~0 s
                    if os.path.splitext(it['ruta'])[1].lower() in EXT_IMAGEN:
                        media.add_option(f":image-duration={it['duracion']}")
                    lista_vlc.add_media(media)
                reproductor_lista.set_media_list(lista_vlc)
                reproductor_lista.play()
                firma_actual = firma
                estado_reporte['reproduciendo'] = ", ".join(disponibles)
            elif not items:
                # Nada descargado aún: mantener lo que haya sonando; si no hay nada,
                # mostrar el default mientras tanto.
                _aviso('sin_archivos', "Ningún archivo programado disponible aún; se reintentará")
                if firma_actual is None:
                    _reproducir_default("contenido programado aún descargando")
            if items:
                _aviso_ok('sin_archivos')

        elif tipo == 'sin_horario':
            # Servidor OK pero sin programación para esta hora → mostrar default.
            if not _reproducir_default("sin programación"):
                if firma_actual is not None:
                    log.info("Sin contenido programado ni default.mp4, deteniendo")
                    reproductor_lista.stop()
                    firma_actual = None
                    estado_reporte['reproduciendo'] = ''

        elif tipo == 'sin_conexion':
            # Sin servidor y sin horario guardado: si ya hay contenido sonando se
            # mantiene en bucle; si no hay nada, cae al default.
            if firma_actual is None:
                _reproducir_default("sin conexión con el servidor")

    def ciclo_totem():
        """Hilo principal: lanza el trabajo de red en segundo plano y reprograma.
        La reprogramación va en 'finally': un error aquí no puede detener el ciclo."""
        try:
            if not trabajo_activo.is_set():
                trabajo_activo.set()
                threading.Thread(target=_trabajo_red, daemon=True, name="ciclo-red").start()
        except Exception:
            trabajo_activo.clear()
            log.exception("Error lanzando el ciclo de red")
        finally:
            root.after(INTERVALO_POLLING_MS, ciclo_totem)

    def procesar_cola():
        """Hilo principal: drena los resultados que dejó el hilo de red."""
        try:
            while True:
                _aplicar_resultado(cola_resultados.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            log.exception("Error aplicando resultado del ciclo")
        finally:
            root.after(500, procesar_cola)

    def vigilar_reproduccion():
        """Hilo principal: si debería haber algo sonando pero VLC lleva varias
        comprobaciones parado o en error, fuerza a reconstruir la playlist."""
        nonlocal firma_actual, paradas_seguidas
        try:
            if firma_actual is not None:
                estado = reproductor_lista.get_state()
                if estado in (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error):
                    paradas_seguidas += 1
                else:
                    paradas_seguidas = 0
                if paradas_seguidas >= 3:
                    log.warning(f"VLC detenido ({estado}) con contenido pendiente; se relanza la reproducción")
                    paradas_seguidas = 0
                    firma_actual = None   # el siguiente ciclo reconstruye la lista
                    estado_reporte['reproduciendo'] = ''
            else:
                paradas_seguidas = 0
        except Exception:
            log.exception("Error en la vigilancia de reproducción")
        finally:
            root.after(INTERVALO_VIGILANCIA_MS, vigilar_reproduccion)

    # Mostrar el default de inmediato (si existe) para que el tótem NUNCA arranque
    # en negro ni dependa del servidor. El primer ciclo lo cambiará a lo programado
    # si hay conexión y horario; si no, se queda con el default. Operación autónoma.
    _reproducir_default("arranque")

    # Arrancar despues de que la ventana este lista
    root.after(1000, ciclo_totem)
    root.after(1200, procesar_cola)
    root.after(15000, vigilar_reproduccion)
    try:
        root.mainloop()
    except Exception:
        try:
            reproductor_lista.stop()
            root.destroy()
        except Exception:
            pass
        raise


def ejecutar_con_reinicio(id_totem, url_servidor, api_key):
    """Si el reproductor cae por un error inesperado, se registra y se vuelve a
    levantar: un tótem sin teclado no puede quedarse en un mensaje de error."""
    while True:
        try:
            iniciar_bucle_totem(id_totem, url_servidor, api_key)
            return   # salida normal (apagado remoto)
        except Exception:
            log.exception("El reproductor se detuvo por un error; se reinicia en 10 s")
            time.sleep(10)


def _salida_emergencia(root, reproductor_lista):
    """Ctrl+Shift+Q para que un tecnico pueda salir del modo kiosko."""
    log.info("Salida de emergencia solicitada")
    reproductor_lista.stop()
    root.destroy()
    sys.exit()


# ==========================================
# INTERFAZ DE PRIMERA CONFIGURACION
# ==========================================
def mostrar_ventana_configuracion():
    ventana = tk.Tk()
    ventana.title("Configuracion Inicial del Totem")
    ventana.geometry("400x360")
    ventana.eval('tk::PlaceWindow . center')

    tk.Label(ventana, text="Sistema de Totems Hexa38", font=("Arial", 14, "bold")).pack(pady=(20, 5))
    tk.Label(ventana, text="Configura este equipo:", font=("Arial", 10)).pack(pady=(0, 15))

    tk.Label(ventana, text="ID de este Totem (Ej: 1):", font=("Arial", 10, "bold")).pack()
    entry_id = tk.Entry(ventana, width=10, justify="center")
    entry_id.pack(pady=5)

    tk.Label(ventana, text="URL del Servidor (Ej: http://192.168.1.50:8080):", font=("Arial", 10, "bold")).pack()
    entry_url = tk.Entry(ventana, width=35, justify="center")
    entry_url.pack(pady=5)
    entry_url.insert(0, "http://")

    tk.Label(ventana, text="API Key del servidor:", font=("Arial", 10, "bold")).pack()
    entry_key = tk.Entry(ventana, width=35, justify="center")
    entry_key.pack(pady=5)
    entry_key.insert(0, API_KEY_POR_DEFECTO)

    def guardar_y_arrancar():
        val_id = entry_id.get().strip()
        val_url = entry_url.get().strip().rstrip('/')
        val_key = entry_key.get().strip()

        if not val_id.isdigit():
            messagebox.showerror("Error", "El ID del Totem debe ser solo un numero.")
            return
        if not val_url.startswith("http"):
            messagebox.showerror("Error", "La URL debe empezar con 'http://'")
            return
        if not val_key:
            messagebox.showerror("Error", "La API Key no puede estar vacia.")
            return

        # Comprobar contra el servidor ANTES de entrar en kiosko: una URL o API Key
        # equivocadas se detectan aquí, con el técnico todavía delante. Se consulta el
        # horario (solo lectura) y no el latido, que consumiría comandos pendientes.
        problema = None
        try:
            resp = requests.get(f"{val_url}/api/horario/{val_id}",
                                headers={'X-API-Key': val_key}, timeout=TIMEOUT_API)
            if resp.status_code != 200:
                problema = _describir_http(resp)
        except requests.exceptions.RequestException as e:
            problema = f"no se pudo conectar con el servidor ({e.__class__.__name__})"
        if problema and not messagebox.askyesno(
                "Problema con el servidor",
                f"La prueba de conexión falló:\n\n{problema}\n\n¿Guardar de todos modos?"):
            return

        configuracion = {"id_totem": int(val_id), "url_servidor": val_url, "api_key": val_key}
        with open(ARCHIVO_CONFIG, "w") as f:
            json.dump(configuracion, f)

        messagebox.showinfo("Exito", "Configuracion guardada.\nEl reproductor iniciara ahora.")
        ventana.destroy()
        ejecutar_con_reinicio(int(val_id), val_url, val_key)

    tk.Button(ventana, text="Guardar e Iniciar", command=guardar_y_arrancar,
              bg="#27ae60", fg="white", font=("Arial", 12, "bold")).pack(pady=20)
    ventana.mainloop()


def cargar_config():
    """Devuelve (id_totem, url_servidor, api_key) o None si la config no sirve."""
    try:
        with open(ARCHIVO_CONFIG, "r") as archivo:
            config = json.load(archivo)
        return (int(config["id_totem"]),
                str(config["url_servidor"]).strip().rstrip('/'),
                str(config.get("api_key") or API_KEY_POR_DEFECTO).strip())
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.error(f"Error leyendo config: {e}")
        return None


if __name__ == "__main__":
    # Antes, cualquier fallo del reproductor en marcha caía en "error leyendo config"
    # y abría la ventana de configuración en plena tienda. Ahora solo se muestra si
    # la configuración realmente falta o está dañada.
    config = cargar_config() if os.path.exists(ARCHIVO_CONFIG) else None
    if config is None:
        mostrar_ventana_configuracion()
    else:
        ejecutar_con_reinicio(*config)
