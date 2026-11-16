FROM python:3.9-slim

WORKDIR /app

# tzdata: para que TZ (ver docker-compose.yml) dé la hora local en panel y logs
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código
COPY . .

# Crear directorios para archivos y datos persistentes
RUN mkdir -p media data

# Exponer puerto de la aplicación
EXPOSE 5000

# Ejecutar con Gunicorn (recomendado para producción)
# --worker-class gthread --threads 8: cada descarga ocupa un hilo, no un worker
#   entero; varias descargas lentas a la vez no bloquean latidos ni el panel, y
#   una descarga larga ya no muere por el timeout del worker.
# --preload: la migración de la base corre una sola vez, no una por worker.
# --access-logfile -: cada petición (incluidos los 401) queda en `docker compose logs`.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "--preload", "--worker-class", "gthread", "--workers", "2", "--threads", "8", "--timeout", "120", "--access-logfile", "-", "servidor:app"]
