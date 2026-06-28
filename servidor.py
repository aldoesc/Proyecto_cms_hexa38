import os
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
UPLOAD_FOLDER = 'media'
ALLOWED_EXTENSIONS = {'mp4', 'jpg', 'jpeg', 'png'}
UMBRAL_ONLINE_SEGUNDOS = 60
API_KEY = os.environ.get('API_KEY', 'hexa38-totem-key-change-me')
if API_KEY == 'hexa38-totem-key-change-me':
    logging.warning("API_KEY usa el valor por defecto. Cámbiala antes de producción.")

@contextmanager
def get_db():
    """Context manager para manejar conexiones a SQLite."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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

        # Migración: añadir columna 'duracion' a BDs creadas antes de esta versión
        columnas = [c['name'] for c in cursor.execute("PRAGMA table_info(contenidos)").fetchall()]
        if 'duracion' not in columnas:
            cursor.execute("ALTER TABLE contenidos ADD COLUMN duracion INTEGER DEFAULT 10")
            conn.commit()
            logging.info("Migración: columna 'duracion' añadida a contenidos")

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

# Crear carpeta de medios si no existe
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

inicializar_bd()

# --- Helpers ---
def registrar_log(totem_id, evento, detalle=None, db=None):
    """Registra un evento en la tabla de logs."""
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        key = request.headers.get('X-API-Key') or request.args.get('api_key')
        if key != API_KEY:
            return jsonify({'error': 'API Key inválida'}), 401
        return f(*args, **kwargs)
    return decorated

# --- CSRF ---
def generar_csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']

app.jinja_env.globals['csrf_token'] = generar_csrf_token
app.config['API_KEY'] = API_KEY

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
@app.route('/')
@login_requerido
def dashboard():
    with get_db() as db:
        totems = db.execute('SELECT * FROM totems').fetchall()
    ahora = datetime.now()
    totems_estado = []
    for t in totems:
        d = dict(t)
        d['online'] = False
        if t['ultimo_latido']:
            try:
                ultimo = datetime.strptime(t['ultimo_latido'], "%Y-%m-%d %H:%M:%S")
                d['online'] = (ahora - ultimo).total_seconds() < UMBRAL_ONLINE_SEGUNDOS
                d['ultimo_latido_formato'] = ultimo.strftime("%H:%M:%S - %d/%m/%Y")
            except ValueError:
                d['ultimo_latido_formato'] = t['ultimo_latido']
        totems_estado.append(d)
    return render_template('dashboard.html', totems=totems_estado)

@app.route('/totems', methods=['GET', 'POST'])
@login_requerido
def gestion_totems():
    if request.method == 'POST':
        nombre = request.form.get('nombre')
        ip = request.form.get('ip')
        with get_db() as db:
            cursor = db.execute('INSERT INTO totems (nombre, ip) VALUES (?, ?)', (nombre, ip))
            registrar_log(cursor.lastrowid, 'registro', f'Totem registrado: {nombre} ({ip})', db=db)
            db.commit()
        flash('Equipo registrado', 'success')
        return redirect(url_for('gestion_totems'))
    with get_db() as db:
        totems = db.execute('SELECT * FROM totems').fetchall()
    return render_template('totems.html', totems=totems)

@app.route('/editar_totem/<int:id>', methods=['POST'])
@login_requerido
def editar_totem(id):
    nombre = request.form.get('nombre')
    ip = request.form.get('ip')
    with get_db() as db:
        db.execute('UPDATE totems SET nombre = ?, ip = ? WHERE id = ?', (nombre, ip, id))
        db.commit()
    flash('Equipo actualizado', 'success')
    return redirect(url_for('gestion_totems'))

@app.route('/eliminar_totem/<int:id>', methods=['POST'])
@login_requerido
def eliminar_totem(id):
    with get_db() as db:
        db.execute('DELETE FROM programacion WHERE totem_id = ?', (id,))
        db.execute('DELETE FROM totems WHERE id = ?', (id,))
        db.execute('DELETE FROM logs WHERE totem_id = ?', (id,))
        db.commit()
    flash('Equipo eliminado', 'success')
    return redirect(url_for('gestion_totems'))

@app.route('/apagar_totem/<int:id>', methods=['POST'])
@login_requerido
def apagar_totem(id):
    with get_db() as db:
        db.execute('UPDATE totems SET comando = "apagar" WHERE id = ?', (id,))
        db.commit()
    registrar_log(id, 'apagado', 'Orden de apagado remoto enviada')
    flash('Comando de apagado enviado al equipo', 'info')
    return redirect(url_for('dashboard'))

@app.route('/subir_contenido', methods=['GET', 'POST'])
@login_requerido
def subir_contenido():
    if request.method == 'POST':
        archivo = request.files.get('archivo')
        tipo = request.form.get('tipo', 'video')

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

        with get_db() as db:
            existe = db.execute('SELECT id FROM contenidos WHERE nombre_archivo = ?', (nombre,)).fetchone()
            if existe:
                return jsonify({'error': 'Ya existe un archivo con ese nombre'}), 400

            archivo.save(ruta)
            db.execute('INSERT INTO contenidos (nombre_archivo, tipo, duracion) VALUES (?, ?, ?)',
                       (nombre, tipo, duracion))
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
            db.execute('DELETE FROM programacion WHERE contenido_id = ?', (id,))
            db.commit()
            flash('Archivo eliminado', 'success')
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
        if hora_inicio and hora_fin and hora_inicio >= hora_fin:
            flash('La hora de inicio debe ser anterior a la hora de fin', 'danger')
            return redirect(url_for('programacion'))
        with get_db() as db:
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
    datos = request.json or {}
    almacenamiento = datos.get('almacenamiento', 0)
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as db:
        totem = db.execute('SELECT comando, ultimo_latido FROM totems WHERE id = ?', (id,)).fetchone()
        if not totem:
            return jsonify({'error': 'Totem no registrado'}), 404

        if not totem['ultimo_latido']:
            registrar_log(id, 'conexion', 'Primera conexion del totem', db=db)

        db.execute('UPDATE totems SET ultimo_latido = ?, almacenamiento = ? WHERE id = ?',
                   (ahora, almacenamiento, id))
        comando = totem['comando'] if totem else 'ninguno'
        if comando != 'ninguno':
            db.execute('UPDATE totems SET comando = "ninguno" WHERE id = ?', (id,))
            registrar_log(id, 'comando', f'Comando enviado: {comando}', db=db)
        db.commit()

    return jsonify({"comando": comando})

@app.route('/api/horario/<int:id>')
@api_key_requerida
def api_horario(id):
    """Devuelve todos los horarios programados para un tótem."""
    with get_db() as db:
        rows = db.execute('''
            SELECT c.nombre_archivo, c.tipo, c.duracion, p.hora_inicio, p.hora_fin
            FROM programacion p
            JOIN contenidos c ON p.contenido_id = c.id
            WHERE p.totem_id = ?
            ORDER BY p.hora_inicio
        ''', (id,)).fetchall()

    programacion = []
    for row in rows:
        programacion.append({
            'nombre_archivo': row['nombre_archivo'],
            'url_descarga': f"/api/descargar/{row['nombre_archivo']}",
            'duracion': row['duracion'] if row['duracion'] else 10,
            'hora_inicio': row['hora_inicio'],
            'hora_fin': row['hora_fin']
        })
    return jsonify(programacion)

@app.route('/api/descargar/<filename>')
@api_key_requerida
def api_descargar(filename):
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=False)

# --- API estado en tiempo real (para AJAX del dashboard) ---
@app.route('/api/estado_totems')
@login_requerido
def api_estado_totems():
    """Endpoint interno para refrescar el dashboard sin recargar la pagina."""
    with get_db() as db:
        totems = db.execute('SELECT * FROM totems').fetchall()
    ahora = datetime.now()
    resultado = []
    for t in totems:
        online = False
        ultimo_formato = 'Sin conexión'
        if t['ultimo_latido']:
            try:
                ultimo = datetime.strptime(t['ultimo_latido'], "%Y-%m-%d %H:%M:%S")
                online = (ahora - ultimo).total_seconds() < UMBRAL_ONLINE_SEGUNDOS
                ultimo_formato = ultimo.strftime("%H:%M:%S - %d/%m/%Y")
            except ValueError:
                ultimo_formato = t['ultimo_latido']
        resultado.append({
            'id': t['id'],
            'nombre': t['nombre'],
            'ultimo_latido': ultimo_formato,
            'almacenamiento': t['almacenamiento'],
            'online': online
        })
    return jsonify(resultado)

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