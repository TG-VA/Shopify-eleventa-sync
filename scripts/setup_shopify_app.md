# 🛒 Guía: Crear una Aplicación Personalizada en Shopify

> **Objetivo:** Obtener las credenciales API (Access Token y Webhook Secret) necesarias para que el middleware de sincronización se conecte a tu tienda Shopify.

---

## Requisitos Previos

- Tener una tienda Shopify activa (plan Basic o superior)
- Acceso como **propietario** de la tienda o como colaborador con permisos de administración
- El middleware ya debe estar desplegado (necesitarás la URL para configurar webhooks)

---

## Paso 1: Acceder a la Configuración de Apps

1. Inicia sesión en tu panel de administración de Shopify:
   ```
   https://tu-tienda.myshopify.com/admin
   ```
2. En el menú lateral izquierdo, haz clic en **⚙️ Configuración** (esquina inferior izquierda)
3. En el menú de configuración, selecciona **Apps y canales de venta**
4. Haz clic en **Desarrollar apps** (parte superior de la página)

> ⚠️ Si no ves la opción "Desarrollar apps", necesitas habilitarla primero (ver Paso 2).

---

## Paso 2: Habilitar el Desarrollo de Apps Personalizadas

> Este paso solo es necesario si es la primera vez que creas una app personalizada en tu tienda.

1. En la página de "Desarrollar apps", verás un mensaje indicando que el desarrollo personalizado no está habilitado
2. Haz clic en **Permitir desarrollo de apps personalizadas**
3. Lee la advertencia y haz clic en **Permitir desarrollo de apps personalizadas** para confirmar
4. Ahora tendrás acceso al botón "Crear una app"

---

## Paso 3: Crear la Aplicación "Eleventa Sync"

1. Haz clic en el botón **Crear una app**
2. En el formulario que aparece:
   - **Nombre de la app:** `Eleventa Sync`
   - **Desarrollador de la app:** Selecciona tu usuario (el propietario de la tienda)
3. Haz clic en **Crear app**

---

## Paso 4: Configurar los Permisos (Scopes) del Admin API

1. Una vez creada la app, estarás en la página de configuración de la app
2. Haz clic en **Configurar los ámbitos del Admin API**
3. En la lista de permisos, busca y activa los siguientes scopes:

| Scope | Descripción | Uso en la sincronización |
|-------|-------------|--------------------------|
| `read_inventory` | Leer niveles de inventario | Consultar stock actual en Shopify |
| `write_inventory` | Escribir niveles de inventario | Actualizar stock desde Eleventa |
| `read_products` | Leer productos | Obtener catálogo para mapeo |
| `read_orders` | Leer pedidos | Detectar ventas para ajustar stock |

4. **Importante:** No actives permisos innecesarios. Seguimos el principio de mínimo privilegio.
5. Haz clic en **Guardar** en la parte superior

---

## Paso 5: Instalar la Aplicación

1. Después de guardar los permisos, ve a la pestaña **Resumen** de la app
2. Haz clic en **Instalar app**
3. Revisa los permisos que se otorgarán y haz clic en **Instalar**
4. **¡IMPORTANTE!** Se mostrará el **Admin API access token** (formato: `shpat_xxxxx...`)

> 🔴 **COPIA ESTE TOKEN INMEDIATAMENTE.** Solo se muestra UNA VEZ.
> Si lo pierdes, tendrás que desinstalar y reinstalar la app para generar uno nuevo.

5. Guarda el token de forma segura:
   - Pégalo en tu archivo `.env` como valor de `SHOPIFY_ACCESS_TOKEN`
   - **NUNCA** lo compartas en chats, correos o repositorios públicos

---

## Paso 6: Obtener el ID de Ubicación

El ID de ubicación es necesario para actualizar niveles de inventario en la ubicación correcta.

1. Ve a **Configuración** → **Ubicaciones**
2. Haz clic en tu ubicación principal
3. En la URL del navegador, verás algo como:
   ```
   https://admin.shopify.com/store/tu-tienda/settings/locations/12345678901
   ```
4. El número final (`12345678901`) es tu `SHOPIFY_LOCATION_ID`
5. Cópialo a tu archivo `.env`

---

## Paso 7: Configurar Webhooks

Los webhooks permiten que Shopify notifique al middleware en tiempo real cuando ocurren eventos.

### 7.1 Configurar la URL de webhooks

1. En la configuración de tu app personalizada, ve a la pestaña **Admin API**
2. Desplázate hasta la sección **Suscripciones a webhooks de eventos**
3. Haz clic en **Agregar suscripción**

### 7.2 Crear los webhooks necesarios

Repite el proceso para cada uno de estos eventos:

| Evento (Topic) | URL del Endpoint | Formato |
|-----------------|-------------------|---------|
| `inventory_levels/update` | `https://tu-middleware.railway.app/webhooks/shopify/inventory` | JSON |
| `orders/create` | `https://tu-middleware.railway.app/webhooks/shopify/orders` | JSON |
| `products/update` | `https://tu-middleware.railway.app/webhooks/shopify/products` | JSON |

Para cada webhook:
1. Selecciona el **evento** de la lista desplegable
2. Ingresa la **URL** de tu middleware (reemplaza con tu dominio real)
3. Selecciona formato **JSON**
4. Haz clic en **Guardar**

### 7.3 Obtener el Webhook Signing Secret

1. En la sección de webhooks, busca **Secreto de firma del webhook** (o "Webhook signing secret")
2. Haz clic en el ícono de ojo 👁️ para revelarlo
3. Copia el valor (formato: `whsec_xxxxx...`)
4. Pégalo en tu archivo `.env` como valor de `SHOPIFY_WEBHOOK_SECRET`

> 💡 Este secreto se usa para verificar que los webhooks realmente provienen de Shopify
> (verificación HMAC-SHA256). Es fundamental para la seguridad.

---

## Paso 8: Verificar la Configuración

Después de completar todos los pasos, tu archivo `.env` debe tener estos valores configurados:

```env
SHOPIFY_STORE_NAME=tu-tienda
SHOPIFY_ACCESS_TOKEN=shpat_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SHOPIFY_API_VERSION=2024-10
SHOPIFY_WEBHOOK_SECRET=whsec_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SHOPIFY_LOCATION_ID=12345678901
```

### Prueba rápida con curl

Verifica que tu token funciona ejecutando este comando (reemplaza los valores):

```bash
curl -s -H "X-Shopify-Access-Token: TU_ACCESS_TOKEN" \
  "https://tu-tienda.myshopify.com/admin/api/2024-10/products/count.json"
```

Deberías recibir una respuesta como:
```json
{ "count": 42 }
```

Si recibes un error `401`, revisa que el token sea correcto y que la app esté instalada.

---

## Resumen de Credenciales Obtenidas

| Variable | Dónde obtenerla | Formato |
|----------|-----------------|---------|
| `SHOPIFY_STORE_NAME` | URL de tu tienda | `mi-tienda` (sin `.myshopify.com`) |
| `SHOPIFY_ACCESS_TOKEN` | Al instalar la app (Paso 5) | `shpat_xxxx...` |
| `SHOPIFY_API_VERSION` | Documentación de Shopify | `2024-10` |
| `SHOPIFY_WEBHOOK_SECRET` | Configuración de webhooks (Paso 7.3) | `whsec_xxxx...` |
| `SHOPIFY_LOCATION_ID` | URL de ubicaciones (Paso 6) | Número largo |

---

## Solución de Problemas

| Problema | Solución |
|----------|----------|
| No veo "Desarrollar apps" | Asegúrate de ser el propietario de la tienda |
| Perdí el access token | Desinstala la app y vuelve a instalarla |
| Los webhooks no llegan | Verifica que la URL sea pública y con HTTPS |
| Error 403 en la API | Revisa que los scopes sean correctos |
| Error 429 (rate limit) | Reduce la frecuencia de llamadas a la API |

---

> 📌 **Nota:** Guarda este documento como referencia. Si necesitas regenerar credenciales
> en el futuro, repite los pasos 5-7.
