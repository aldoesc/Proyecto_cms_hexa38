import os
import re
import hmac
import hashlib
import secrets
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
from contextlib import contextmanager
import logging

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# SECRET_KEY: si no viene del entorno se genera una aleatoria, pero entonces
# las sesiones se invalidan en cada reinicio. En producción debe fijarse por env.
_secret_env = os.environ.get('SECRET_KEY')
if not _secret_env:
    logging.warning("SECRET_KEY no definida en el entorno: se generará una temporal "
                    "(las sesiones se cerrarán al reiniciar). Defínela en producción.")
elif _secret_env == 'hexa38_secret_key_change_in_production':
    logging.warning("SECRET_KEY usa el valor por defecto. Cámbiala antes de producción.")
app.secret_key = _secret_env or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB

DATA_DIR = os.environ.get('DATA_DIR', '.')
DATABASE = os.path.join(DATA_DIR, 'cms_totems.db')
UPLOAD_FOLDER = os.path.abspath(os.environ.get('MEDIA_DIR', 'media'))
ALLOWED_EXTENSIONS = {'mp4', 'jpg', 'jpeg', 'png'}
UMBRAL_ONLINE_SEGUNDOS = 60
# Un comando remoto (apagar) que el tótem no recoge en este tiempo se descarta:
# evita que una orden vieja se ejecute horas después, al volver la comunicación.
CADUCIDAD_COMANDO_SEGUNDOS = 300
FORMATO_FECHA = "%Y-%m-%d %H:%M:%S"
API_KEY = os.environ.get('API_KEY', 'hexa38-totem-key-change-me')
if API_KEY == 'hexa38-totem-key-change-me':
    logging.warning("API_KEY usa el valor por defecto. Cámbiala antes de producción.")

@contextmanager
def get_db():
    """Context manager para manejar conexiones a SQLite."""
    # timeout: si otro worker está escribiendo, espera en vez de fallar con "database is locked"
    conn = sqlite3.connect(DATABASE, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def sha256_archivo(ruta):
    h = hashlib.sha256()
    with open(ruta, 'rb') as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b''):
            h.update(bloque)
    return h.hexdigest()

def _asegurar_columna(cursor, tabla, columna, definicion):
    """Migración idempotente: añade la columna si la BD es de una versión anterior."""
    columnas = [c['name'] for c in cursor.execute(f"PRAGMA table_info({tabla})").fetchall()]
    if columna not in columnas:
        try:
            cursor.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}")
            logging.info(f"Migración: columna '{columna}' añadida a {tabla}")
        except sqlite3.OperationalError as e:
            # Otro worker pudo añadirla a la vez
            logging.warning(f"Migración de {tabla}.{columna}: {e}")

def inicializar_bd():
    """Crea las tablas y usuario por defecto si no existen."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS totems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            ip TEXT NOT NULL,
            ultimo_latido TEXT,
            almacenamiento INTEGER DEFAULT 0,
            comando TEXT DEFAULT 'ninguno'
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS contenidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre_archivo TEXT NOT NULL,
            tipo TEXT DEFAULT 'video',
            duracion INTEGER DEFAULT 10
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS programacion (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            totem_id INTEGER,
            contenido_id INTEGER,
            hora_inicio TEXT,
            hora_fin TEXT,
            FOREIGN KEY(totem_id) REFERENCES totems(id),
            FOREIGN KEY(contenido_id) REFERENCES contenidos(id)
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            totem_id INTEGER,
            evento TEXT NOT NULL,
            detalle TEXT,
            FOREIGN KEY(totem_id) REFERENCES totems(id)
        )''')
        conn.commit()

        # Migraciones para BDs creadas antes de esta versión
        _asegurar_columna(cursor, 'contenidos', 'duracion', 'INTEGER DEFAULT 10')
        # Tamaño y huella de cada archivo: el tótem los usa para verificar sus descargas
        _asegurar_columna(cursor, 'contenidos', 'tamano', 'INTEGER')
        _asegurar_columna(cursor, 'contenidos', 'sha256', 'TEXT')
        _asegurar_columna(cursor, 'totems', 'comando_fecha', 'TEXT')
        # Estado que informa el propio tótem en cada latido
        _asegurar_columna(cursor, 'totems', 'version', 'TEXT')
        _asegurar_columna(cursor, 'totems', 'reproduciendo', 'TEXT')
        _asegurar_columna(cursor, 'totems', 'sync', 'TEXT')
        conn.commit()

        # Calcular tamaño/huella de archivos subidos antes de esta versión
        for c in cursor.execute("SELECT id, nombre_archivo FROM contenidos WHERE sha256 IS NULL").fetchall():
            ruta = os.path.join(UPLOAD_FOLDER, c['nombre_archivo'])
            if os.path.isfile(ruta):
                cursor.execute("UPDATE contenidos SET tamano = ?, sha256 = ? WHERE id = ?",
                               (os.path.getsize(ruta), sha256_archivo(ruta), c['id']))
            else:
                logging.warning(f"Contenido #{c['id']} '{c['nombre_archivo']}' está en la BD "
                                f"pero el archivo no existe en {UPLOAD_FOLDER}")
        conn.commit()

        # Usuario por defecto solo si no existe
        cursor.execute("SELECT * FROM usuarios WHERE username = 'admin'")
        admin = cursor.fetchone()
        if not admin:
            cursor.execute("INSERT INTO usuarios (username, password) VALUES (?, ?)",
                           ('admin', generate_password_hash('admin123')))
            conn.commit()
            logging.info("Usuario admin creado con contraseña por defecto. Cámbiala en producción.")
        else:
            # Migrar contraseñas en texto plano a hash
            usuarios = cursor.execute("SELECT id, password FROM usuarios").fetchall()
            for u in usuarios:
                if not u['password'].startswith(('pbkdf2:', 'scrypt:')):
                    cursor.execute("UPDATE usuarios SET password = ? WHERE id = ?",
                                   (generate_password_hash(u['password']), u['id']))
            conn.commit()

# Crear carpeta de medios si no existe (antes de inicializar: la migración la lee)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

inicializar_bd()

# --- Helpers ---
def registrar_log(totem_id, evento, detalle=None, db=None):
    """Registra un evento en la tabla de logs."""
    ahora = datetime.now().strftime(FORMATO_FECHA)
    if db:
        db.execute('INSERT INTO logs (fecha, totem_id, evento, detalle) VALUES (?, ?, ?, ?)',
                   (ahora, totem_id, evento, detalle))
    else:
        with get_db() as conn:
            conn.execute('INSERT INTO logs (fecha, totem_id, evento, detalle) VALUES (?, ?, ?, ?)',
                         (ahora, totem_id, evento, detalle))
            conn.commit()

def api_key_requerida(f):
    """Decorador que verifica la API Key en los endpoints de los totems."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-API-Key') or request.args.get('api_key') or ''
        if not hmac.compare_digest(key.encode('utf-8'), API_KEY.encode('utf-8')):
            # Visible en `docker compose logs`: un tótem mal configurado ya no falla en silencio
            logging.warning(f"401 API Key {'ausente' if not key else 'incorrecta'}: "
                            f"{request.method} {request.path} desde {request.remote_addr}")
            return jsonify({'error': 'API Key inválida'}), 401
        return f(*args, **kwargs)
    return decorated

# --- CSRF ---
def generar_csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']

app.jinja_env.globals['csrf_token'] = generar_csrf_token

@app.before_request
def verificar_csrf():
    if request.method == 'POST' and not request.path.startswith('/api/'):
        token = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
        if not token or token != session.get('csrf_token'):
            flash('Token de seguridad inválido. Intenta de nuevo.', 'danger')
            return redirect(request.referrer or url_for('login'))

# --- Decorador de autenticación ---
def login_requerido(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            flash('Por favor inicia sesión para acceder a esta página', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- Rutas de autenticación ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('usuario')
        password = request.form.get('password')
        with get_db() as db:
            user = db.execute('SELECT * FROM usuarios WHERE username = ?', (username,)).fetchone()
        if user and check_password_hash(user['password'], password):
            session['user'] = username
            flash('Inicio de sesión exitoso', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Usuario o contraseña incorrectos', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user', None)
    flash('Sesión cerrada', 'info')
    return redirect(url_for('login'))

# --- Rutas principales ---
def estado_totems():
    """Estado de todos los tótems para el dashboard (render inicial y refresco AJAX)."""
    with get_db() as db:
        totems = db.execute('SELECT * FROM totems').fetchall()
    ahora = datetime.now()
    resultado = []
    for t in totems:
        online = False
        ultimo_formato = 'Sin conexión'
        if t['ultimo_latido']:
            try:
                ultimo = datetime.strptime(t['ultimo_latido'], FORMATO_FECHA)
                online = abs((ahora - ultimo).total_seconds()) < UMBRAL_ONLINE_SEGUNDOS
                ultimo_formato = ultimo.strftime("%H:%M:%S - %d/%m/%Y")
            except ValueError:
                ultimo_formato = t['ultimo_latido']
        resultado.append({
            'id': t['id'],
            'nombre': t['nombre'],
            'ultimo_latido': ultimo_formato,
            'almacenamiento': t['almacenamiento'] or 0,
            'online': online,
            'version': t['version'] or '',
            'reproduciendo': t['reproduciendo'] or '',
            'sync': t['sync'] or '',
            'comando_pendiente': (t['comando'] or 'ninguno') != 'ninguno',
        })
    return resultado

@app.route('/')
@login_requerido
def dashboard():
    return render_template('dashboard.html', totems=estado_totems())

@app.route('/totems', methods=['GET', 'POST'])
@login_requerido
def gestion_totems():
    if request.method == 'POST':
        nombre = (request.form.get('nombre') or '').strip()
        ip = (request.form.get('ip') or '').strip()
        if not nombre or not ip:
            flash('Nombre e IP son obligatorios', 'danger')
            return redirect(url_for('gestion_totems'))
        with get_db() as db:
            cursor = db.execute('INSERT INTO totems (nombre, ip) VALUES (?, ?)', (nombre, ip))
            registrar_log(cursor.lastrowid, 'registro', f'Totem registrado: {nombre} ({ip})', db=db)
            db.commit()
        flash('Equipo registrado', 'success')
        return redirect(url_for('gestion_totems'))
    with get_db() as db:
        totems = db.execute('''
            SELECT t.*, (SELECT COUNT(*) FROM programacion p WHERE p.totem_id = t.id) AS n_horarios
            FROM totems t''').fetchall()
    return render_template('totems.html', totems=totems)

@app.route('/editar_totem/<int:id>', methods=['POST'])
@login_requerido
def editar_totem(id):
    nombre = (request.form.get('nombre') or '').strip()
    ip = (request.form.get('ip') or '').strip()
    if not nombre or not ip:
        flash('Nombre e IP son obligatorios', 'danger')
        return redirect(url_for('gestion_totems'))
    with get_db() as db:
        db.execute('UPDATE totems SET nombre = ?, ip = ? WHERE id = ?', (nombre, ip, id))
        db.commit()
    flash('Equipo actualizado', 'success')
    return redirect(url_for('gestion_totems'))

@app.route('/eliminar_totem/<int:id>', methods=['POST'])
@login_requerido
def eliminar_totem(id):
    with get_db() as db:
        totem = db.execute('SELECT nombre, ip FROM totems WHERE id = ?', (id,)).fetchone()
        if not totem:
            flash('Equipo no encontrado', 'danger')
            return redirect(url_for('gestion_totems'))
        n = db.execute('DELETE FROM programacion WHERE totem_id = ?', (id,)).rowcount
        db.execute('DELETE FROM totems WHERE id = ?', (id,))
        # Los logs del tótem NO se borran: son el único rastro de lo ocurrido.
        registrar_log(None, 'eliminacion',
                      f"Tótem #{id} '{totem['nombre']}' ({totem['ip']}) eliminado por {session.get('user')}; "
                      f"se borraron {n} horario(s) de su programación", db=db)
        db.commit()
    flash(f'Equipo eliminado junto con {n} horario(s) programados', 'success')
    return redirect(url_for('gestion_totems'))

@app.route('/apagar_totem/<int:id>', methods=['POST'])
@login_requerido
def apagar_totem(id):
    ahora = datetime.now().strftime(FORMATO_FECHA)
    with get_db() as db:
        db.execute("UPDATE totems SET comando = 'apagar', comando_fecha = ? WHERE id = ?", (ahora, id))
        registrar_log(id, 'apagado', f"Orden de apagado remoto enviada por {session.get('user')}", db=db)
        db.commit()
    flash(f'Comando de apagado enviado. Si el equipo no lo recoge en '
          f'{CADUCIDAD_COMANDO_SEGUNDOS // 60} min, se descarta.', 'info')
    return redirect(url_for('dashboard'))

@app.route('/subir_contenido', methods=['GET', 'POST'])
@login_requerido
def subir_contenido():
    if request.method == 'POST':
        archivo = request.files.get('archivo')

        # Duración en segundos (solo aplica a imágenes). Acotada a un rango sano.
        try:
            duracion = int(request.form.get('duracion', 10))
        except (TypeError, ValueError):
            duracion = 10
        duracion = max(1, min(duracion, 3600))

        if not archivo or archivo.filename == '':
            return jsonify({'error': 'No se seleccionó ningún archivo'}), 400

        if not allowed_file(archivo.filename):
            return jsonify({'error': 'Formato no permitido. Usa .mp4, .jpg o .png'}), 400

        nombre = secure_filename(archivo.filename)
        if not nombre:
            return jsonify({'error': 'Nombre de archivo no válido'}), 400
        ruta = os.path.join(UPLOAD_FOLDER, nombre)
        # El tipo se deduce de la extensión real, no de lo que diga el formulario
        tipo = 'video' if nombre.rsplit('.', 1)[-1].lower() == 'mp4' else 'imagen'

        with get_db() as db:
            existe = db.execute('SELECT id FROM contenidos WHERE nombre_archivo = ?', (nombre,)).fetchone()
            if existe:
                return jsonify({'error': 'Ya existe un archivo con ese nombre'}), 400

            # Se escribe a un temporal y se renombra al terminar: una subida cortada no
            # deja un archivo a medias publicado. De paso se calcula tamaño y SHA-256.
            ruta_tmp = ruta + '.subiendo'
            huella = hashlib.sha256()
            tamano = 0
            try:
                with open(ruta_tmp, 'wb') as f:
                    while True:
                        bloque = archivo.stream.read(1024 * 1024)
                        if not bloque:
                            break
                        f.write(bloque)
                        huella.update(bloque)
                        tamano += len(bloque)
                if tamano == 0:
                    raise ValueError('archivo vacío')
                os.replace(ruta_tmp, ruta)
            except (OSError, ValueError) as e:
                if os.path.exists(ruta_tmp):
                    os.remove(ruta_tmp)
                logging.error(f"Fallo guardando {nombre}: {e}")
                return jsonify({'error': f'No se pudo guardar el archivo ({e})'}), 500

            db.execute('INSERT INTO contenidos (nombre_archivo, tipo, duracion, tamano, sha256) '
                       'VALUES (?, ?, ?, ?, ?)', (nombre, tipo, duracion, tamano, huella.hexdigest()))
            db.commit()
        registrar_log(None, 'subida', f'Archivo subido: {nombre} ({tipo})')
        return jsonify({'success': True}), 200

    return render_template('subir_contenido.html')

@app.route('/archivos')
@login_requerido
def biblioteca():
    with get_db() as db:
        archivos = db.execute('SELECT * FROM contenidos').fetchall()
    return render_template('archivos.html', archivos=archivos)

@app.route('/media/<path:filename>')
@login_requerido
def media_panel(filename):
    """Previsualización/descarga para el panel, con la sesión del usuario. Así la
    API Key de los tótems no aparece en el HTML ni en el historial del navegador."""
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/editar_duracion/<int:id>', methods=['POST'])
@login_requerido
def editar_duracion(id):
    try:
        duracion = int(request.form.get('duracion', 10))
    except (TypeError, ValueError):
        duracion = 10
    duracion = max(1, min(duracion, 3600))
    with get_db() as db:
        db.execute('UPDATE contenidos SET duracion = ? WHERE id = ?', (duracion, id))
        db.commit()
    flash('Duración actualizada', 'success')
    return redirect(url_for('biblioteca'))

@app.route('/eliminar_archivo/<int:id>', methods=['POST'])
@login_requerido
def eliminar_archivo(id):
    with get_db() as db:
        archivo = db.execute('SELECT nombre_archivo FROM contenidos WHERE id = ?', (id,)).fetchone()
        if archivo:
            ruta = os.path.join(UPLOAD_FOLDER, archivo['nombre_archivo'])
            if os.path.exists(ruta):
                os.remove(ruta)
            db.execute('DELETE FROM contenidos WHERE id = ?', (id,))
            n = db.execute('DELETE FROM programacion WHERE contenido_id = ?', (id,)).rowcount
            registrar_log(None, 'eliminacion',
                          f"Archivo '{archivo['nombre_archivo']}' eliminado por {session.get('user')}; "
                          f"se quitaron {n} horario(s) que lo usaban", db=db)
            db.commit()
            flash(f'Archivo eliminado (estaba en {n} horario(s))' if n else 'Archivo eliminado', 'success')
        else:
            flash('Archivo no encontrado', 'danger')
    return redirect(url_for('biblioteca'))

# --- Programación ---
@app.route('/programacion', methods=['GET', 'POST'])
@login_requerido
def programacion():
    if request.method == 'POST':
        totem_id = request.form.get('totem_id')
        contenido_id = request.form.get('contenido_id')
        hora_inicio = request.form.get('hora_inicio')
        hora_fin = request.form.get('hora_fin')
        formato_hora = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
        if not (formato_hora.match(hora_inicio or '') and formato_hora.match(hora_fin or '')):
            flash('Indica hora de inicio y fin en formato HH:MM', 'danger')
            return redirect(url_for('programacion'))
        if hora_inicio >= hora_fin:
            flash('La hora de inicio debe ser anterior a la hora de fin', 'danger')
            return redirect(url_for('programacion'))
        with get_db() as db:
            if not db.execute('SELECT 1 FROM totems WHERE id = ?', (totem_id,)).fetchone() or \
               not db.execute('SELECT 1 FROM contenidos WHERE id = ?', (contenido_id,)).fetchone():
                flash('El equipo o el contenido seleccionado ya no existe', 'danger')
                return redirect(url_for('programacion'))
            # Detectar solapamiento con horarios existentes del mismo tótem.
            # Dos rangos se solapan si: inicio_A < fin_B y inicio_B < fin_A.
            solapados = db.execute('''
                SELECT c.nombre_archivo, p.hora_inicio, p.hora_fin
                FROM programacion p
                JOIN contenidos c ON p.contenido_id = c.id
                WHERE p.totem_id = ? AND ? < p.hora_fin AND p.hora_inicio < ?
            ''', (totem_id, hora_inicio, hora_fin)).fetchall()

            db.execute('''INSERT INTO programacion (totem_id, contenido_id, hora_inicio, hora_fin)
                          VALUES (?, ?, ?, ?)''', (totem_id, contenido_id, hora_inicio, hora_fin))
            db.commit()
        if solapados:
            nombres = ', '.join(s['nombre_archivo'] for s in solapados)
            flash(f'Programación guardada. Aviso: el horario se solapa con {nombres}; '
                  f'el tótem reproducirá ambos en bucle durante ese intervalo.', 'warning')
        else:
            flash('Programación guardada', 'success')
        return redirect(url_for('programacion'))

    with get_db() as db:
        totems = db.execute('SELECT * FROM totems').fetchall()
        contenidos = db.execute('SELECT * FROM contenidos').fetchall()
        programaciones = db.execute('''
            SELECT p.id, p.totem_id, p.hora_inicio, p.hora_fin,
                   t.nombre as nombre_totem,
                   c.nombre_archivo
            FROM programacion p
            JOIN totems t ON p.totem_id = t.id
            JOIN contenidos c ON p.contenido_id = c.id
            ORDER BY t.nombre, p.hora_inicio
        ''').fetchall()
    return render_template('programacion.html', totems=totems, contenidos=contenidos, programaciones=programaciones)

@app.route('/eliminar_programacion/<int:id>', methods=['POST'])
@login_requerido
def eliminar_programacion(id):
    with get_db() as db:
        db.execute('DELETE FROM programacion WHERE id = ?', (id,))
        db.commit()
    flash('Programación eliminada', 'success')
    return redirect(url_for('programacion'))

# --- Salud del servicio (para Docker healthcheck / monitoreo) ---
@app.route('/api/health')
def api_health():
    try:
        with get_db() as db:
            db.execute('SELECT 1').fetchone()
        return jsonify({'estado': 'ok'}), 200
    except Exception as e:
        logging.error(f"Healthcheck falló: {e}")
        return jsonify({'estado': 'error'}), 503

# --- API para los tótems ---
@app.route('/api/latido/<int:id>', methods=['POST'])
@api_key_requerida
def api_latido(id):
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        datos = {}
    try:
        almacenamiento = max(0, min(100, int(datos.get('almacenamiento', 0))))
    except (TypeError, ValueError):
        almacenamiento = 0

    def texto(campo, maximo):
        valor = datos.get(campo)
        return str(valor)[:maximo] if valor else None

    momento = datetime.now()
    ahora = momento.strftime(FORMATO_FECHA)

    with get_db() as db:
        totem = db.execute('SELECT comando, comando_fecha, ultimo_latido FROM totems WHERE id = ?',
                           (id,)).fetchone()
        if not totem:
            logging.warning(f"Latido de un tótem no registrado: #{id} desde {request.remote_addr}")
            return jsonify({'error': 'Totem no registrado'}), 404

        if not totem['ultimo_latido']:
            registrar_log(id, 'conexion', 'Primera conexion del totem', db=db)
        else:
            # Dejar constancia de los cortes: ayuda a diagnosticar redes inestables
            try:
                silencio = (momento - datetime.strptime(totem['ultimo_latido'], FORMATO_FECHA)).total_seconds()
                if silencio > UMBRAL_ONLINE_SEGUNDOS:
                    registrar_log(id, 'reconexion',
                                  f'Vuelve a estar en línea tras {int(silencio // 60)} min '
                                  f'{int(silencio % 60)} s sin latido', db=db)
            except ValueError:
                pass

        db.execute('''UPDATE totems SET ultimo_latido = ?, almacenamiento = ?,
                      version = ?, reproduciendo = ?, sync = ? WHERE id = ?''',
                   (ahora, almacenamiento, texto('version', 20), texto('reproduciendo', 300),
                    texto('sync', 300), id))
        comando = totem['comando'] or 'ninguno'
        if comando != 'ninguno':
            db.execute("UPDATE totems SET comando = 'ninguno', comando_fecha = NULL WHERE id = ?", (id,))
            try:
                antiguedad = (momento - datetime.strptime(totem['comando_fecha'], FORMATO_FECHA)).total_seconds()
            except (TypeError, ValueError):
                antiguedad = None   # comando sin fecha (anterior a esta versión): no es fiable
            if antiguedad is None or antiguedad > CADUCIDAD_COMANDO_SEGUNDOS:
                registrar_log(id, 'comando',
                              f"Comando '{comando}' descartado por caducado "
                              f"(emitido: {totem['comando_fecha'] or 'fecha desconocida'})", db=db)
                comando = 'ninguno'
            else:
                registrar_log(id, 'comando', f'Comando enviado: {comando}', db=db)
        db.commit()

    return jsonify({"comando": comando})

@app.route('/api/horario/<int:id>')
@api_key_requerida
def api_horario(id):
    """Devuelve todos los horarios programados para un tótem."""
    with get_db() as db:
        rows = db.execute('''
            SELECT c.nombre_archivo, c.tipo, c.duracion, c.tamano, c.sha256,
                   p.hora_inicio, p.hora_fin
            FROM programacion p
            JOIN contenidos c ON p.contenido_id = c.id
            WHERE p.totem_id = ?
            ORDER BY p.hora_inicio, p.id
        ''', (id,)).fetchall()

    programacion = []
    for row in rows:
        programacion.append({
            'nombre_archivo': row['nombre_archivo'],
            'url_descarga': f"/api/descargar/{row['nombre_archivo']}",
            'tipo': row['tipo'],
            'duracion': row['duracion'] if row['duracion'] else 10,
            # Con esto el tótem comprueba que su copia está completa y es la correcta
            'tamano': row['tamano'],
            'sha256': row['sha256'],
            'hora_inicio': row['hora_inicio'],
            'hora_fin': row['hora_fin']
        })
    return jsonify(programacion)

@app.route('/api/descargar/<filename>')
@api_key_requerida
def api_descargar(filename):
    # conditional=True (por defecto) atiende peticiones Range: el tótem puede
    # reanudar una descarga cortada en vez de empezarla de nuevo.
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=False)

# --- API estado en tiempo real (para AJAX del dashboard) ---
@app.route('/api/estado_totems')
@login_requerido
def api_estado_totems():
    """Endpoint interno para refrescar el dashboard sin recargar la pagina."""
    return jsonify(estado_totems())

# --- Logs de actividad ---
@app.route('/logs')
@login_requerido
def ver_logs():
    POR_PAGINA = 50
    try:
        pagina = max(1, int(request.args.get('pagina', 1)))
    except (TypeError, ValueError):
        pagina = 1
    offset = (pagina - 1) * POR_PAGINA
    with get_db() as db:
        total = db.execute('SELECT COUNT(*) AS n FROM logs').fetchone()['n']
        logs = db.execute('''
            SELECT l.id, l.fecha, l.evento, l.detalle, l.totem_id,
                   t.nombre as nombre_totem
            FROM logs l
            LEFT JOIN totems t ON l.totem_id = t.id
            ORDER BY l.id DESC
            LIMIT ? OFFSET ?
        ''', (POR_PAGINA, offset)).fetchall()
    total_paginas = max(1, (total + POR_PAGINA - 1) // POR_PAGINA)
    return render_template('logs.html', logs=logs, pagina=pagina, total_paginas=total_paginas, total=total)

@app.route('/limpiar_logs', methods=['POST'])
@login_requerido
def limpiar_logs():
    with get_db() as db:
        db.execute('DELETE FROM logs')
        db.commit()
    flash('Historial de actividad limpiado', 'success')
    return redirect(url_for('ver_logs'))

@app.route('/cambiar_password', methods=['GET', 'POST'])
@login_requerido
def cambiar_password():
    if request.method == 'POST':
        password_actual = request.form.get('password_actual')
        password_nueva = request.form.get('password_nueva')
        password_confirmar = request.form.get('password_confirmar')

        if password_nueva != password_confirmar:
            flash('Las contraseñas nuevas no coinciden', 'danger')
            return redirect(url_for('cambiar_password'))

        with get_db() as db:
            user = db.execute('SELECT * FROM usuarios WHERE username = ?',
                            (session['user'],)).fetchone()
            if user and check_password_hash(user['password'], password_actual):
                db.execute('UPDATE usuarios SET password = ? WHERE username = ?',
                          (generate_password_hash(password_nueva), session['user']))
                db.commit()
                flash('Contraseña actualizada correctamente', 'success')
                return redirect(url_for('dashboard'))
            else:
                flash('Contraseña actual incorrecta', 'danger')

    return render_template('cambiar_password.html')   

# --- Inicio del servidor ---
if __name__ == '__main__':
    # Usar Gunicorn en producción, pero para desarrollo local usamos Flask
    app.run(host='0.0.0.0', port=5000, debug=False)