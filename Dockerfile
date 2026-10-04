# ==============================================================================
# Shopify-Eleventa Sync Middleware — Dockerfile
# Imagen multi-etapa optimizada para producción
# ==============================================================================

# ---------------------
# Etapa 1: Dependencias
# ---------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# Instalar dependencias del sistema necesarias para compilar paquetes nativos
# (asyncpg requiere compilación, firebird-driver necesita libfbclient)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    firebird-dev \
    && rm -rf /var/lib/apt/lists/*

# Copiar archivo de proyecto e instalar dependencias
COPY pyproject.toml README.md ./
<<<<<<< HEAD
COPY agent agent/
COPY middleware middleware/
COPY shared shared/
=======
>>>>>>> origin/main
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install .

# -------------------------
# Etapa 2: Imagen de Producción
# -------------------------
FROM python:3.11-slim AS production

# Metadatos de la imagen
LABEL maintainer="Equipo de Desarrollo"
LABEL description="Middleware de sincronización Shopify ↔ Eleventa"
LABEL version="1.0.0"

# Variables de entorno por defecto
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    APP_PORT=8000 \
    APP_ENV=production

WORKDIR /app

# Instalar solo las bibliotecas de runtime necesarias (sin compiladores)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libfbclient2 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar las dependencias ya compiladas desde la etapa builder
COPY --from=builder /install /usr/local

# Crear usuario no-root para seguridad
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home appuser

# Copiar el código fuente de la aplicación
COPY . .

# Cambiar al usuario no-root
USER appuser

# Exponer el puerto de la aplicación
EXPOSE ${APP_PORT}

# Healthcheck para orquestadores de contenedores
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${APP_PORT}/health || exit 1

# Comando de inicio: Uvicorn con workers configurables
CMD ["sh", "-c", "uvicorn middleware.main:app --host 0.0.0.0 --port ${APP_PORT} --workers ${WORKERS:-2} --log-level ${LOG_LEVEL:-info}"]
