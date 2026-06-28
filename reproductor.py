import time
import requests
import os
import json
import sys
import shutil
import logging
import queue
import threading
import subprocess
from logging.handlers import RotatingFileHandler
from datetime import datetime
import tkinter as tk
from tkinter import messagebox

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
ARCHIVO_POR_DEFECTO = "default.mp4"
INTERVALO_POLLING_MS = 10000  # 10 segundos
API_KEY_POR_DEFECTO = "hexa38-totem-key-change-me"

if not os.path.exists(CARPETA_LOCAL):
    os.makedirs(CARPETA_LOCAL)


# ==========================================
# DESCARGA CON STREAMING (no carga todo en RAM)
# ==========================================
def descargar_archivo(nombre_archivo, url_completa, api_key):
    ruta_final = os.path.join(CARPETA_LOCAL, nombre_archivo)
    # Si existe y tiene contenido, no re-descargar.
    # Tamaño 0 indica descarga interrumpida — se descarta y se reintenta.
    if os.path.exists(ruta_final) and os.path.getsize(ruta_final) > 0:
        return ruta_final
    if os.path.exists(ruta_final):
        log.warning(f"Archivo truncado detectado, re-descargando: {nombre_archivo}")
        os.remove(ruta_final)
    log.info(f"Descargando: {nombre_archivo}...")
    headers = {'X-API-Key': api_key}
    ruta_tmp = ruta_final + ".tmp"
    try:
        # timeout=(conexion, lectura): 5 s para conectar, 120 s por lectura de chunk
        respuesta = requests.get(url_completa, stream=True, timeout=(5, 120), headers=headers)
        if respuesta.status_code == 200:
            with open(ruta_tmp, 'wb') as f:
                for chunk in respuesta.iter_content(chunk_size=65536):
                    f.write(chunk)
            os.rename(ruta_tmp, ruta_final)
            log.info(f"Descarga completa: {nombre_archivo}")
            return ruta_final
        else:
            log.warning(f"Error HTTP {respuesta.status_code} al descargar {nombre_archivo}")
    except requests.exceptions.RequestException as e:
        log.error(f"Error de red descargando {nombre_archivo}: {e}")
    finally:
        if os.path.exists(ruta_tmp):
            os.remove(ruta_tmp)
    return None


# ==========================================
# REPRODUCTOR KIOSKO CON PANTALLA COMPLETA
# ==========================================
def iniciar_bucle_totem(id_totem, url_servidor, api_key=API_KEY_POR_DEFECTO):
    log.info(f"Iniciando Totem ID: {id_totem}")
    log.info(f"Servidor: {url_servidor}")
    headers = {'X-API-Key': api_key}

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
    lista_actual = []
    # Cola para pasar resultados del hilo de red al hilo principal de Tkinter.
    # tkinter NO es thread-safe: TODA operación de UI/VLC ocurre en el hilo principal.
    cola_resultados = queue.Queue()
    # Evita solapar ciclos de red: si una descarga lenta sigue en curso, no se lanza
    # otro hilo encima (evitaría descargas duplicadas del mismo archivo que se pisan).
    trabajo_activo = threading.Event()

    def _reproducir_default(motivo=""):
        """Reproduce default.mp4 (si existe localmente) para que el tótem opere de
        forma autónoma sin depender del servidor. No reinicia si el default ya está
        sonando. Devuelve True si el default está disponible en disco."""
        nonlocal lista_actual
        ruta_defecto = os.path.join(CARPETA_LOCAL, ARCHIVO_POR_DEFECTO)
        if not os.path.exists(ruta_defecto):
            return False
        if lista_actual != [ARCHIVO_POR_DEFECTO]:
            log.info(f"Reproduciendo default.mp4{(' (' + motivo + ')') if motivo else ''}")
            lista_vlc = instancia_vlc.media_list_new([ruta_defecto])
            reproductor_lista.set_media_list(lista_vlc)
            reproductor_lista.play()
            lista_actual = [ARCHIVO_POR_DEFECTO]
        return True

    def _trabajo_red():
        """Envoltura del hilo de red: libera el candado de ciclo al terminar,
        incluso si ocurre un error inesperado."""
        try:
            _trabajo_red_impl()
        finally:
            trabajo_activo.clear()

    def _trabajo_red_impl():
        """Se ejecuta en un hilo aparte. Solo hace red y descargas (operaciones
        lentas); nunca toca Tkinter ni VLC. Deja el resultado en la cola."""
        resultado = {'tipo': 'nada'}

        # 1. LATIDO
        comando = 'ninguno'
        try:
            disco = shutil.disk_usage(os.path.abspath(os.sep))
            porcentaje_uso = int((disco.used / disco.total) * 100)
            resp = requests.post(
                f"{url_servidor}/api/latido/{id_totem}",
                json={"almacenamiento": porcentaje_uso},
                headers=headers,
                timeout=5
            )
            if resp.status_code == 200:
                comando = resp.json().get("comando", "ninguno")
        except requests.exceptions.RequestException as e:
            log.warning(f"Error en latido: {e}")

        # 2. APAGADO REMOTO
        if comando == "apagar":
            cola_resultados.put({'tipo': 'apagar'})
            return

        # 3. PLAYLIST POR HORARIO
        try:
            ahora = datetime.now().strftime("%H:%M")
            respuesta = requests.get(f"{url_servidor}/api/horario/{id_totem}", headers=headers, timeout=8)

            tareas_actuales = []
            if respuesta.status_code == 200:
                for tarea in respuesta.json():
                    if tarea['hora_inicio'] <= ahora <= tarea['hora_fin']:
                        tareas_actuales.append(tarea)

            if tareas_actuales:
                nombres_archivos = [t['nombre_archivo'] for t in tareas_actuales]
                items = []
                for tarea in tareas_actuales:
                    url_descarga = f"{url_servidor}{tarea['url_descarga']}"
                    ruta_local = descargar_archivo(tarea['nombre_archivo'], url_descarga, api_key)
                    if ruta_local:
                        items.append({
                            'nombre': tarea['nombre_archivo'],
                            'ruta': ruta_local,
                            'duracion': int(tarea.get('duracion') or 10),
                        })
                resultado = {'tipo': 'playlist', 'nombres': nombres_archivos, 'items': items}
            else:
                resultado = {'tipo': 'sin_horario'}
        except requests.exceptions.RequestException as e:
            log.warning(f"Error obteniendo horario: {e}")
            resultado = {'tipo': 'sin_conexion'}

        cola_resultados.put(resultado)

    def _aplicar_resultado(resultado):
        """Se ejecuta en el hilo principal. Aquí sí se puede tocar VLC/Tkinter."""
        nonlocal lista_actual
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
            # Solo los archivos YA descargados forman la lista. Comparar contra los
            # DISPONIBLES (no contra los programados) evita reconstruir/reiniciar la
            # lista en cada ciclo mientras un video grande todavía se está descargando.
            # Comparación por conjunto: insensible al orden, solo reacciona a cambios reales.
            disponibles = [it['nombre'] for it in items]
            if items and set(disponibles) != set(lista_actual):
                log.info(f"Nueva playlist: {disponibles}")
                lista_vlc = instancia_vlc.media_list_new()
                for it in items:
                    media = instancia_vlc.media_new(it['ruta'])
                    # Las imágenes no tienen duración propia; VLC las pasaría en ~0 s
                    ext = os.path.splitext(it['ruta'])[1].lower()
                    if ext in ('.jpg', '.jpeg', '.png', '.bmp', '.gif'):
                        media.add_option(f":image-duration={it['duracion']}")
                    lista_vlc.add_media(media)
                reproductor_lista.set_media_list(lista_vlc)
                reproductor_lista.play()
                lista_actual = disponibles
            elif not items:
                # Nada descargado aún: mantener lo que haya sonando y reintentar luego.
                log.warning("Ningún archivo programado disponible aún; se reintentará")

        elif tipo == 'sin_horario':
            # Servidor OK pero sin programación para esta hora → mostrar default.
            if not _reproducir_default("sin programación"):
                if lista_actual != []:
                    log.info("Sin contenido programado ni default.mp4, deteniendo")
                    reproductor_lista.stop()
                    lista_actual = []

        elif tipo == 'sin_conexion':
            # Servidor/red caídos: el tótem sigue operando solo. Si ya hay contenido
            # sonando (programado o default), se mantiene en bucle sin interrupción.
            # Si no hay nada (ej. arranque sin servidor), cae al default.
            if not lista_actual:
                _reproducir_default("sin conexión con el servidor")

    def ciclo_totem():
        """Hilo principal: lanza el trabajo de red en segundo plano y reprograma.
        Si el ciclo anterior sigue en curso (descarga lenta), no lanza otro encima."""
        if not trabajo_activo.is_set():
            trabajo_activo.set()
            threading.Thread(target=_trabajo_red, daemon=True).start()
        root.after(INTERVALO_POLLING_MS, ciclo_totem)

    def procesar_cola():
        """Hilo principal: drena los resultados que dejó el hilo de red."""
        try:
            while True:
                _aplicar_resultado(cola_resultados.get_nowait())
        except queue.Empty:
            pass
        root.after(500, procesar_cola)

    # Mostrar el default de inmediato (si existe) para que el tótem NUNCA arranque
    # en negro ni dependa del servidor. El primer ciclo lo cambiará a lo programado
    # si hay conexión y horario; si no, se queda con el default. Operación autónoma.
    _reproducir_default("arranque")

    # Arrancar despues de que la ventana este lista
    root.after(1000, ciclo_totem)
    root.after(1200, procesar_cola)
    root.mainloop()


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

        configuracion = {"id_totem": int(val_id), "url_servidor": val_url, "api_key": val_key}
        with open(ARCHIVO_CONFIG, "w") as f:
            json.dump(configuracion, f)

        messagebox.showinfo("Exito", "Configuracion guardada.\nEl reproductor iniciara ahora.")
        ventana.destroy()
        iniciar_bucle_totem(int(val_id), val_url, val_key)

    tk.Button(ventana, text="Guardar e Iniciar", command=guardar_y_arrancar,
              bg="#27ae60", fg="white", font=("Arial", 12, "bold")).pack(pady=20)
    ventana.mainloop()


if __name__ == "__main__":
    if os.path.exists(ARCHIVO_CONFIG):
        try:
            with open(ARCHIVO_CONFIG, "r") as archivo:
                config = json.load(archivo)
            iniciar_bucle_totem(
                config["id_totem"],
                config["url_servidor"],
                config.get("api_key", API_KEY_POR_DEFECTO)
            )
        except Exception as e:
            log.error(f"Error leyendo config: {e}")
            mostrar_ventana_configuracion()
    else:
        mostrar_ventana_configuracion()
