FROM python:3.9-slim

WORKDIR /app

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
# --timeout 300: permite servir videos grandes por redes lentas sin que el worker
#   muera a los 30s (default) y corte la descarga del tótem.
# --workers 3: una descarga lenta no bloquea los latidos de otros tótems/dashboard.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "--timeout", "300", "--workers", "3", "servidor:app"]