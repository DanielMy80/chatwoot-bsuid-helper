import os
import re
import sqlite3
import requests
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

WHATSAPP_API_VER   = os.getenv("WHATSAPP_API_VERSION", "v22.0")
WHATSAPP_PHONE_ID  = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_TOKEN     = os.getenv("WHATSAPP_ACCESS_TOKEN")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "")
DB_PATH            = os.getenv("DB_PATH", "/app/data/bsuid.db")

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS bsuid_tracking (
            conversation_id INTEGER PRIMARY KEY,
            account_id      INTEGER NOT NULL,
            source_id       TEXT NOT NULL,
            requested_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def has_whatsapp_username(sender_data) -> bool:
    """Detecta si el contacto tiene username de WhatsApp activado."""
    if not sender_data:
        return False
    additional = sender_data.get("additional_attributes") or {}
    if additional.get("social_whatsapp_user_name"):
        return True
    social = additional.get("social_profiles") or {}
    if social.get("whatsapp"):
        return True
    return False

def message_contains_trigger(content: str) -> bool:
    """Verifica si el mensaje contiene la palabra clave 'Chatwoot' (case-insensitive)."""
    if not content:
        return False
    return "chatwoot" in content.lower()

def send_request_contact_info(to: str):
    url = f"https://graph.facebook.com/{WHATSAPP_API_VER}/{WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "request_contact_info",
            "body": {
                "text": "Hola 👋 Para poder responderte y brindarte una mejor atención, por favor comparte tu número de teléfono tocando el botón de abajo."
            },
            "action": {"name": "request_contact_info"}
        }
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        print(f"[ERROR] send_request_contact_info: {e}")
        return {"error": str(e)}

@app.route('/', methods=['GET'])
def root():
    return jsonify({"status": "ok", "service": "bsuid-helper"}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200

@app.route('/webhook/chatwoot', methods=['POST'])
def chatwoot_webhook():
    data = request.get_json(force=True, silent=True) or {}
    event = data.get("event")

    print(f"[DEBUG] Webhook recibido - event: {event}")

    if WEBHOOK_SECRET:
        token = request.args.get("secret") or request.headers.get("X-Chatwoot-Secret", "")
        if token != WEBHOOK_SECRET:
            print(f"[DEBUG] Secret no coincide")
            return jsonify({"error": "Unauthorized"}), 401

    if event != "message_created":
        print(f"[DEBUG] Ignorado: event no es message_created")
        return jsonify({"status": "ignored", "reason": "not_message_created"}), 200

    if data.get("message_type") != "incoming":
        print(f"[DEBUG] Ignorado: message_type no es incoming ({data.get('message_type')})")
        return jsonify({"status": "ignored", "reason": "not_incoming"}), 200

    account_id      = data.get("account", {}).get("id")
    conversation_id = data.get("conversation", {}).get("id")
    sender          = data.get("sender") or {}
    content         = data.get("content") or ""

    print(f"[DEBUG] Account: {account_id}, Conversation: {conversation_id}")
    print(f"[DEBUG] Content: '{content}'")
    print(f"[DEBUG] Sender data: {sender}")

    if not account_id or not conversation_id:
        return jsonify({"status": "ignored", "reason": "missing_ids"}), 200

    # Paso 1: Verificar si el contacto tiene username de WhatsApp
    if not has_whatsapp_username(sender):
        print(f"[DEBUG] Ignorado: contacto no tiene username de WhatsApp")
        return jsonify({"status": "ignored", "reason": "no_whatsapp_username"}), 200

    print(f"[DEBUG] Contacto tiene username de WhatsApp ✓")

    # Paso 2: Verificar si el mensaje contiene la palabra clave "Chatwoot"
    if not message_contains_trigger(content):
        print(f"[DEBUG] Ignorado: mensaje no contiene la palabra clave 'Chatwoot'")
        return jsonify({"status": "ignored", "reason": "no_trigger_word"}), 200

    print(f"[DEBUG] Mensaje contiene palabra clave 'Chatwoot' ✓")

    # Obtener source_id (wa_id) para enviar el mensaje
    conversation_data = data.get("conversation") or {}
    contact_inbox = conversation_data.get("contact_inbox") or {}
    source_id = contact_inbox.get("source_id")

    if not source_id:
        print(f"[DEBUG] Ignorado: no hay source_id")
        return jsonify({"status": "ignored", "reason": "no_source_id"}), 200

    print(f"[DEBUG] source_id: {source_id}")

    # Evitar spam: solo solicitar una vez por conversación
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM bsuid_tracking WHERE conversation_id = ?", (conversation_id,))
    if c.fetchone():
        conn.close()
        print(f"[DEBUG] Ignorado: ya se solicitó anteriormente para conv {conversation_id}")
        return jsonify({"status": "ignored", "reason": "already_requested"}), 200

    # Enviar mensaje interactivo nativo de WhatsApp
    result = send_request_contact_info(source_id)
    
    c.execute("""
        INSERT INTO bsuid_tracking (conversation_id, account_id, source_id)
        VALUES (?, ?, ?)
    """, (conversation_id, account_id, source_id))
    conn.commit()
    conn.close()

    print(f"[OK] Solicitud enviada a {source_id} (conv={conversation_id}), respuesta: {result}")
    return jsonify({"status": "requested", "wa_response": result}), 200

if __name__ == '__main__':
    init_db()
    port = int(os.getenv("PORT", "5000"))
    app.run(host='0.0.0.0', port=port)
