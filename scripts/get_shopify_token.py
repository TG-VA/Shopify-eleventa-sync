#!/usr/bin/env python3
"""
Script para obtener el Access Token de Shopify mediante OAuth.

Uso:
  1. Configura las variables abajo (STORE, CLIENT_ID, CLIENT_SECRET)
  2. Ejecuta: python get_shopify_token.py
  3. Se abrirá tu navegador para autorizar la app
  4. El token se mostrará en la terminal

Requisitos:
  pip install flask requests
"""

import os
import sys
import secrets
import webbrowser
import threading
import time

try:
    from flask import Flask, request as flask_request
    import requests
except ImportError:
    print("❌ Necesitas instalar Flask y requests:")
    print("   pip install flask requests")
    sys.exit(1)


# ============================================
# ⚠️  CONFIGURA ESTOS VALORES
# ============================================
STORE = "crokets-mx"  # Tu subdominio de Shopify (sin .myshopify.com)
CLIENT_ID = "dac0a982f7cc933f15a4c0f7aec0b259"
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
# Permisos que necesitamos
SCOPES = "read_inventory,write_inventory,read_products,read_orders"

# Puerto local para capturar el callback
PORT = 3456
REDIRECT_URI = f"http://localhost:{PORT}/callback"
# ============================================

# Generar nonce para seguridad
NONCE = secrets.token_hex(16)

# Flask app mínima para capturar el callback
app = Flask(__name__)
access_token_result = {"token": None, "error": None}


@app.route("/callback")
def callback():
    """Captura el código de autorización y lo intercambia por un token."""
    code = flask_request.args.get("code")
    state = flask_request.args.get("state")
    error = flask_request.args.get("error")

    if error:
        access_token_result["error"] = error
        return f"""
        <html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#1a1a2e;color:white">
        <h1>❌ Error</h1>
        <p>{error}</p>
        <p>Puedes cerrar esta ventana.</p>
        </body></html>
        """

    if not code:
        access_token_result["error"] = "No se recibió código de autorización"
        return "<h1>Error: No code received</h1>"

    # Verificar nonce
    if state != NONCE:
        access_token_result["error"] = "State/nonce no coincide"
        return "<h1>Error: State mismatch</h1>"

    # Intercambiar código por access token
    try:
        response = requests.post(
            f"https://{STORE}.myshopify.com/admin/oauth/access_token",
            json={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("access_token", "")
        access_token_result["token"] = token

        # Imprimir el token INMEDIATAMENTE en la terminal
        print()
        print("╔══════════════════════════════════════════════════════════╗")
        print("║  ✅  ¡ACCESS TOKEN OBTENIDO EXITOSAMENTE!               ║")
        print("╠══════════════════════════════════════════════════════════╣")
        print(f"║  Token: {token}")
        print("║                                                          ║")
        print("║  Copia esto en tu archivo .env:                          ║")
        print(f"║  SHOPIFY_ACCESS_TOKEN={token}")
        print("║                                                          ║")
        print("║  ⚠️  GUARDA ESTE TOKEN EN UN LUGAR SEGURO               ║")
        print("╚══════════════════════════════════════════════════════════╝")
        sys.stdout.flush()

        # Apagar el servidor después de responder
        threading.Thread(target=shutdown_server, daemon=True).start()

        return f"""
        <html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f3460;color:white">
        <h1>✅ ¡Token obtenido!</h1>
        <p style="font-size:14px;color:#aaa">El token se mostró en tu terminal.</p>
        <p style="font-size:14px;color:#aaa">Puedes cerrar esta ventana.</p>
        </body></html>
        """
    except Exception as exc:
        access_token_result["error"] = str(exc)
        return f"""
        <html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#1a1a2e;color:white">
        <h1>❌ Error al obtener token</h1>
        <p>{exc}</p>
        </body></html>
        """


def shutdown_server():
    """Apaga el servidor Flask después de obtener el token."""
    time.sleep(2)
    os._exit(0)


def main():
    """Flujo principal de OAuth."""
    global CLIENT_SECRET
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║       🔑  OBTENER ACCESS TOKEN DE SHOPIFY               ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print(f"║  Tienda:     {STORE}.myshopify.com")
    print(f"║  Client ID:  {CLIENT_ID[:20]}...")
    print(f"║  Scopes:     {SCOPES}")
    print(f"║  Callback:   {REDIRECT_URI}")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    if not CLIENT_SECRET:
        print("🔑 Necesitas ingresar tu Client Secret.")
        print("   (Encuéntralo en Dev Dashboard → Settings → Credentials → 👁️)")
        print()
        CLIENT_SECRET = input("   Pega tu Client Secret aquí: ").strip()
        if not CLIENT_SECRET:
            print("❌ No se ingresó ningún secreto. Abortando.")
            sys.exit(1)

    # Construir URL de autorización
    auth_url = (
        f"https://{STORE}.myshopify.com/admin/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&scope={SCOPES}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&state={NONCE}"
    )

    print("📌 IMPORTANTE: Asegúrate de que la URL de redirección esté")
    print(f"   configurada en tu app del Dev Dashboard como:")
    print(f"   {REDIRECT_URI}")
    print()
    print("🌐 Abriendo navegador para autorización...")
    print(f"   Si no se abre automáticamente, copia esta URL:")
    print(f"   {auth_url}")
    print()
    print("⏳ Esperando autorización...")
    print()

    # Abrir navegador
    webbrowser.open(auth_url)

    # Arrancar servidor Flask en modo silencioso
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)

    try:
        app.run(host="localhost", port=PORT, debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        if access_token_result["token"]:
            token = access_token_result["token"]
            print()
            print("╔══════════════════════════════════════════════════════════╗")
            print("║  ✅  ¡ACCESS TOKEN OBTENIDO EXITOSAMENTE!               ║")
            print("╠══════════════════════════════════════════════════════════╣")
            print(f"║  Token: {token[:20]}...{token[-10:]}")
            print("║                                                          ║")
            print("║  Copia esto en tu archivo .env:                          ║")
            print(f"║  SHOPIFY_ACCESS_TOKEN={token}")
            print("║                                                          ║")
            print("║  ⚠️  GUARDA ESTE TOKEN EN UN LUGAR SEGURO               ║")
            print("║  NO lo compartas ni lo subas a un repositorio público    ║")
            print("╚══════════════════════════════════════════════════════════╝")
        elif access_token_result["error"]:
            print(f"\n❌ Error: {access_token_result['error']}")


if __name__ == "__main__":
    main()
