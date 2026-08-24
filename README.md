# 🔄 Shopify-Eleventa Sync

> **Sincronización bidireccional de inventario entre Shopify y Eleventa POS**

Middleware que mantiene sincronizados los niveles de inventario entre tu tienda en línea (Shopify) y tu punto de venta físico (Eleventa), de forma automática y en tiempo real.

---

## 📋 Tabla de Contenidos

- [Descripción](#descripción)
- [Arquitectura](#arquitectura)
- [Requisitos](#requisitos)
- [Inicio Rápido](#inicio-rápido)
- [Configuración](#configuración)
- [Cómo Funciona la Sincronización](#cómo-funciona-la-sincronización)
- [Sincronización Inicial](#sincronización-inicial)
- [Despliegue](#despliegue)
- [Solución de Problemas](#solución-de-problemas)
- [Seguridad](#seguridad)
- [Preguntas Frecuentes](#preguntas-frecuentes)

---

## Descripción

**Shopify-Eleventa Sync** es un middleware desarrollado en Python (FastAPI) que actúa como puente entre dos sistemas:

| Sistema | Tipo | Rol |
|---------|------|-----|
| **Shopify** | Tienda en línea (e-commerce) | Ventas por internet |
| **Eleventa** | Punto de Venta (POS) | Ventas en tienda física |

### ¿Qué problema resuelve?

Cuando vendes tanto en línea como en tienda física, los niveles de inventario se dessincronizan rápidamente. Este middleware:

- ✅ **Actualiza Shopify** cuando se vende o recibe mercancía en Eleventa
- ✅ **Registra ventas online** cuando se hace un pedido en Shopify
- ✅ **Previene sobreventa** al mantener ambos sistemas sincronizados
- ✅ **Opera en tiempo real** usando webhooks + polling

---

## Arquitectura

```
┌──────────────────┐          ┌────────────────────────────────┐          ┌──────────────────┐
│                  │          │     MIDDLEWARE (FastAPI)        │          │                  │
│     SHOPIFY      │◄────────►│                                │◄────────►│    ELEVENTA      │
│   (Tienda Web)   │ Webhooks │  ┌──────────┐  ┌───────────┐  │ Polling  │  (Punto de Venta)│
│                  │  + REST  │  │ Webhooks │  │  Polling   │  │ Firebird │                  │
│  • Productos     │  API     │  │ Handler  │  │  Service   │  │   DB     │  • Artículos     │
│  • Inventario    │          │  └────┬─────┘  └─────┬──────┘  │          │  • Existencias   │
│  • Pedidos       │          │       │              │         │          │  • Ventas        │
│                  │          │       ▼              ▼         │          │                  │
└──────────────────┘          │  ┌────────────────────────┐    │          └──────────────────┘
                              │  │     Sync Engine        │    │
                              │  │  (Lógica de negocio)   │    │
                              │  └───────────┬────────────┘    │
                              │              │                 │
                              │  ┌───────────▼────────────┐    │
                              │  │     PostgreSQL          │    │
                              │  │  • Mapeos de productos  │    │
                              │  │  • Cola de cambios      │    │
                              │  │  • Historial de sync    │    │
                              │  └────────────────────────┘    │
                              └────────────────────────────────┘
```

### Flujo de datos

1. **Shopify → Middleware:** Shopify envía webhooks cuando cambian inventarios o se crean pedidos
2. **Middleware → Eleventa:** El motor de polling revisa periódicamente cambios en Firebird
3. **PostgreSQL:** Almacena mapeos de productos, cola de operaciones pendientes e historial

---

## Requisitos

### Software

| Requisito | Versión mínima | Notas |
|-----------|---------------|-------|
| Python | 3.11+ | Requerido |
| PostgreSQL | 14+ | Base de datos del middleware |
| Firebird | 2.5+ / 3.0+ | Base de datos de Eleventa |
| Eleventa | 5.0+ | Punto de venta |
| Shopify | Plan Basic+ | Tienda en línea |

### Acceso de Red

- El middleware debe poder conectarse al servidor Firebird de Eleventa (puerto 3050)
- El middleware debe ser accesible desde internet (para recibir webhooks de Shopify)
- Acceso a la API de Shopify (Admin REST API)

---

## Inicio Rápido

### Paso 1: Clonar el repositorio

```bash
git clone https://github.com/tu-usuario/shopify-eleventa-sync.git
cd shopify-eleventa-sync
```

### Paso 2: Crear entorno virtual e instalar dependencias

```bash
python -m venv .venv
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows

pip install -e ".[dev]"
```

### Paso 3: Configurar variables de entorno

```bash
cp .env.example .env
# Edita .env con tu editor favorito y completa TODOS los valores
```

> 📖 Consulta la [guía de configuración de Shopify](scripts/setup_shopify_app.md) para obtener las credenciales API.

### Paso 4: Preparar la base de datos PostgreSQL

```bash
# Crear la base de datos (si no existe)
createdb shopify_eleventa_sync
```

### Paso 5: Ejecutar la sincronización inicial

```bash
python scripts/initial_sync.py
```

Este script:
1. Lee todos los productos de Eleventa y Shopify
2. Los empareja automáticamente por código de barras, SKU o nombre
3. Te pide confirmar los emparejamientos dudosos
4. Guarda los mapeos en PostgreSQL
5. Genera un reporte detallado en `sync_reports/`

### Paso 6: Iniciar el middleware

```bash
uvicorn app.main:app --reload --port 8000
```

El servidor estará disponible en `http://localhost:8000`.

> 🔗 Documentación interactiva de la API: `http://localhost:8000/docs`

---

## Configuración

Todas las variables se configuran en el archivo `.env`. Consulta [`.env.example`](.env.example) para ver todas las opciones disponibles con documentación detallada.

### Variables Principales

| Variable | Descripción | Ejemplo |
|----------|-------------|---------|
| `SHOPIFY_STORE_NAME` | Nombre de tu tienda Shopify | `mi-tienda` |
| `SHOPIFY_ACCESS_TOKEN` | Token de la app personalizada | `shpat_xxxx` |
| `SHOPIFY_WEBHOOK_SECRET` | Secreto para verificar webhooks | `whsec_xxxx` |
| `SHOPIFY_LOCATION_ID` | ID de la ubicación principal | `12345678901` |
| `ELEVENTA_DB_HOST` | IP del servidor Firebird | `192.168.1.100` |
| `ELEVENTA_DB_PATH` | Ruta a la base de datos | `C:/Eleventa/Data/ELEVENTA.FDB` |
| `DATABASE_URL` | Conexión a PostgreSQL | `postgresql://user:pass@host/db` |

### Variables de Sincronización

| Variable | Descripción | Por defecto |
|----------|-------------|-------------|
| `SYNC_POLL_INTERVAL_SECONDS` | Intervalo de polling a Eleventa | `300` (5 min) |
| `SYNC_MAX_RETRIES` | Reintentos máximos por operación | `3` |
| `SYNC_BATCH_SIZE` | Tamaño de lote para sync masiva | `50` |
| `SYNC_NAME_MATCH_THRESHOLD` | Umbral mínimo para match por nombre | `0.85` |

---

## Cómo Funciona la Sincronización

### Dirección 1: Eleventa → Shopify

```
Eleventa (cambio de stock) → Polling Service → Sync Engine → Shopify API
```

1. **Polling Service** consulta la base de datos Firebird de Eleventa cada `N` segundos
2. Compara las existencias actuales con las últimas conocidas
3. Si detecta cambios, envía las actualizaciones al **Sync Engine**
4. El Sync Engine busca el mapeo correspondiente en PostgreSQL
5. Llama a la **Shopify Inventory API** para actualizar el nivel de inventario

### Dirección 2: Shopify → Middleware (→ registro)

```
Shopify (venta/webhook) → Webhook Handler → Sync Engine → PostgreSQL
```

1. Shopify envía un **webhook** cuando se crea un pedido o cambia el inventario
2. El **Webhook Handler** verifica la firma HMAC-SHA256 para autenticidad
3. Procesa el evento y actualiza el registro de sincronización en PostgreSQL
4. El cambio queda registrado para que Eleventa lo refleje en su próximo polling

### Resolución de Conflictos

Cuando ambos sistemas reportan un cambio simultáneo para el mismo producto:

1. Se aplica la estrategia **"último cambio gana"** (last-write-wins)
2. Se registra el conflicto en la tabla `sync_conflicts` para auditoría
3. Se puede configurar una prioridad por sistema (Eleventa o Shopify)

---

## Sincronización Inicial

Antes de activar la sincronización continua, debes ejecutar el script de sincronización inicial para mapear los productos entre ambos sistemas.

```bash
python scripts/initial_sync.py
```

### Estrategias de Emparejamiento

El script usa tres estrategias en orden de prioridad:

| Prioridad | Estrategia | Confianza | Confirmación |
|-----------|-----------|-----------|--------------|
| 1 | Código de barras exacto | 100% | Automática |
| 2 | SKU/Clave exacto | 100% | Automática |
| 3 | Similitud de nombre | Variable | Manual |

### Reportes

Los reportes se generan en la carpeta `sync_reports/` en formatos JSON y CSV con:

- Productos emparejados y tipo de coincidencia
- Productos de Eleventa sin emparejar
- Productos de Shopify sin emparejar
- Estadísticas y porcentajes

---

## Despliegue

### Opción A: Railway (Recomendado)

1. Conecta tu repositorio en [Railway](https://railway.app)
2. Agrega un servicio PostgreSQL
3. Configura las variables de entorno
4. Railway usará el `Dockerfile` y `railway.toml` automáticamente

### Opción B: Docker

```bash
# Construir la imagen
docker build -t shopify-eleventa-sync .

# Ejecutar el contenedor
docker run -d \
  --name sync-middleware \
  --env-file .env \
  -p 8000:8000 \
  shopify-eleventa-sync
```

### Opción C: Servidor VPS

```bash
# Instalar dependencias
pip install -e .

# Ejecutar con Uvicorn
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

> ⚠️ **Importante:** Para recibir webhooks de Shopify, tu middleware debe ser accesible con HTTPS. Usa un proxy reverso como Nginx o Caddy con certificado SSL.

---

## Solución de Problemas

### Error de conexión a Firebird

```
Error al conectar con Eleventa: connection to server was refused
```

**Solución:**
- Verifica que el servicio Firebird esté corriendo en el servidor de Eleventa
- Confirma que el puerto 3050 esté abierto en el firewall
- Revisa las credenciales en `.env`

### Error 401 en Shopify API

```
Error de Shopify API: 401 Unauthorized
```

**Solución:**
- Regenera el token de acceso (desinstala y reinstala la app personalizada)
- Verifica que `SHOPIFY_STORE_NAME` sea correcto (sin `.myshopify.com`)

### Los webhooks no llegan

**Solución:**
- Verifica que tu middleware sea accesible desde internet
- Usa `https://` (SSL obligatorio para webhooks de Shopify)
- Revisa la configuración de webhooks en Shopify Admin
- Consulta el log de webhooks: Shopify Admin → Configuración → Notificaciones

### Productos no se emparejan

**Solución:**
- Verifica que los códigos de barras en Eleventa coincidan con los de Shopify
- Baja el umbral de similitud: `SYNC_NAME_MATCH_THRESHOLD=0.7`
- Ejecuta `initial_sync.py` nuevamente para revisar los emparejamientos

### Error de conexión a PostgreSQL

```
Error de conexión a PostgreSQL: connection refused
```

**Solución:**
- Verifica que PostgreSQL esté corriendo
- Confirma que el `DATABASE_URL` sea correcto
- Revisa que el usuario tenga permisos sobre la base de datos

---

## Seguridad

### Prácticas implementadas

- 🔒 **Verificación HMAC-SHA256** de todos los webhooks de Shopify
- 🔒 **Variables de entorno** para todas las credenciales (nunca en código)
- 🔒 **Mínimo privilegio** en scopes de Shopify API
- 🔒 **Usuario no-root** en contenedor Docker
- 🔒 **HTTPS obligatorio** para endpoints de webhooks
- 🔒 **Conexión cifrada** a PostgreSQL (SSL recomendado)

### Recomendaciones adicionales

1. **Rota el Access Token** de Shopify periódicamente
2. **No expongas** el servidor Firebird a internet — usa VPN o túnel SSH
3. **Activa logging** para auditoría de todas las operaciones de sincronización
4. **Haz backups** regulares de la base de datos PostgreSQL
5. **Monitorea** los errores con servicios como Sentry o el webhook de Slack

---

## Preguntas Frecuentes

### ¿Con qué frecuencia se sincroniza el inventario?

- **Shopify → Middleware:** En tiempo real (webhooks, ~1-3 segundos)
- **Eleventa → Shopify:** Cada 5 minutos por defecto (configurable con `SYNC_POLL_INTERVAL_SECONDS`)

### ¿Qué pasa si el middleware se cae?

Los webhooks de Shopify se reintentan automáticamente por 48 horas. Al reiniciar el middleware, procesará los webhooks pendientes. Los cambios en Eleventa se detectan en el siguiente ciclo de polling.

### ¿Funciona con múltiples ubicaciones en Shopify?

Actualmente soporta una ubicación principal. Para múltiples ubicaciones, se requiere configurar un `SHOPIFY_LOCATION_ID` por ubicación.

### ¿Qué versiones de Eleventa son compatibles?

Se ha probado con Eleventa 5.x y 6.x. Las consultas SQL pueden requerir ajustes según la versión de tu base de datos Firebird.

### ¿Puedo sincronizar precios además de inventario?

El sistema actual está diseñado para sincronización de inventario. La sincronización de precios puede agregarse extendiendo el Sync Engine.

### ¿Cómo manejo productos que solo existen en un sistema?

Los productos sin emparejar aparecen en el reporte de sincronización inicial. Puedes:
1. Crearlos manualmente en el sistema faltante
2. Ignorarlos (solo se sincronizan productos emparejados)
3. Mapearlos manualmente editando la tabla `product_mappings`

### ¿Hay límites en la API de Shopify?

Sí. Shopify aplica rate limiting:
- **REST API:** 40 requests/segundo (bucket de 80 con leak rate)
- **Webhooks:** Sin límite de recepción

El middleware maneja automáticamente los límites con reintentos y backoff exponencial.

---

## Licencia

MIT

---
