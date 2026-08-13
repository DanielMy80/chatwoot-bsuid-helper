import os
import re
import sqlite3
import requests
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

CHATWOOT_URL       = os.getenv("CHATWOOT_URL", "").rstrip("/")
CHATWOOT_API_TOKEN = os.getenv("CHATWOOT_API_TOKEN")
WHATSAPP_API_VER   = os.getenv("WHATSAPP_API_VERSION", "v22.0")
WHATSAPP_PHONE_ID  = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_TOKEN     = os.getenv("WHATSAPP_ACCESS_TOKEN")
CHATWOOT_WEBHOOK   = os.getenv("CHATWOOT_WEBHOOK_URL", "")
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
            contact_id      INTEGER,
            source_id       TEXT NOT NULL,
            requested_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS phone_mappings (
            bsuid          TEXT PRIMARY KEY,
            phone_number   TEXT NOT NULL,
            contact_id     INTEGER,
            account_id     INTEGER,
            updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_bsuid(value: str) -> bool:
    return bool(re.match(r'^[A-Z]{2}\.[A-Z0-9]+$', str(value)))

def chatwoot_headers():
    return {"api_access_token": CHATWOOT_API_TOKEN, "Content-Type": "application/json"}

def get_conversation(account_id: int, conversation_id: int):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}"
    try:
        r = requests.get(url, headers=chatwoot_headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[ERROR] get_conversation: {e}")
    return None

def update_contact_phone(account_id: int, contact_id: int, phone: str):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/contacts/{contact_id}"
    payload = {"phone_number": phone}
    try:
        r = requests.patch(url, json=payload, headers=chatwoot_headers(), timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[ERROR] update_contact_phone: {e}")
    return False

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

@app.route('/webhook/chatwoot', methods=['POST'])
def chatwoot_webhook():
    data = request.get_json(force=True, silent=True) or {}
    event = data.get("event")
    
    # DEBUG: Loggear todo lo que llega
    print(f"[DEBUG] Webhook recibido - event: {event}, msg_type: {data.get('message_type')}, conv: {data.get('conversation', {}).get('id')}")   

    if WEBHOOK_SECRET:
        token = request.args.get("secret") or request.headers.get("X-Chatwoot-Secret", "")
        if token != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

    if event != "message_created":
        return jsonify({"status": "ignored", "reason": "not_message_created"}), 200
    if data.get("message_type") != "incoming":
        return jsonify({"status": "ignored", "reason": "not_incoming"}), 200

    account_id      = data.get("account", {}).get("id")
    conversation_id = data.get("conversation", {}).get("id")
    if not account_id or not conversation_id:
        return jsonify({"status": "ignored", "reason": "missing_ids"}), 200

    conv = get_conversation(account_id, conversation_id)
    if not conv:
        return jsonify({"status": "ignored", "reason": "conv_not_found"}), 200

    contact_inbox = conv.get("meta", {}).get("contact_inbox") or {}
    source_id     = contact_inbox.get("source_id")
    contact_id    = conv.get("meta", {}).get("sender", {}).get("id")

    if not source_id or not is_bsuid(source_id):
        return jsonify({"status": "ignored", "reason": "not_bsuid"}), 200

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM bsuid_tracking WHERE conversation_id = ?", (conversation_id,))
    if c.fetchone():
        conn.close()
        return jsonify({"status": "ignored", "reason": "already_requested"}), 200

    result = send_request_contact_info(source_id)
    c.execute("""
        INSERT INTO bsuid_tracking (conversation_id, account_id, contact_id, source_id)
        VALUES (?, ?, ?, ?)
    """, (conversation_id, account_id, contact_id, source_id))
    conn.commit()
    conn.close()

    print(f"[OK] Solicitud enviada a {source_id} (conv={conversation_id})")
    return jsonify({"status": "requested", "wa_response": result}), 200

@app.route('/webhook/whatsapp', methods=['GET', 'POST'])
def whatsapp_webhook():
    if request.method == 'GET':
        mode = request.args.get("hub.mode")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe":
            return challenge, 200
        return "Forbidden", 403

    data = request.get_json(force=True, silent=True) or {}

    if CHATWOOT_WEBHOOK:
        try:
            requests.post(CHATWOOT_WEBHOOK, json=data, timeout=8)
        except Exception as e:
            print(f"[WARN] No se pudo reenviar a Chatwoot: {e}")

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                if msg.get("type") != "contacts":
                    continue
                from_id = msg.get("from")
                for contact in msg.get("contacts", []):
                    for phone in contact.get("phones", []):
                        wa_id = phone.get("wa_id")
                        if not wa_id or not from_id:
                            continue
                        print(f"[INFO] Contacto recibido: {from_id} -> {wa_id}")
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        c.execute("""
                            SELECT account_id, contact_id FROM bsuid_tracking
                            WHERE source_id = ? ORDER BY requested_at DESC LIMIT 1
                        """, (from_id,))
                        row = c.fetchone()
                        if row:
                            account_id, contact_id = row
                            if update_contact_phone(account_id, contact_id, wa_id):
                                c.execute("""
                                    INSERT OR REPLACE INTO phone_mappings
                                    (bsuid, phone_number, contact_id, account_id)
                                    VALUES (?, ?, ?, ?)
                                """, (from_id, wa_id, contact_id, account_id))
                                conn.commit()
                                print(f"[OK] Contacto {contact_id} actualizado con teléfono {wa_id}")
                            else:
                                print(f"[ERROR] Falló actualización de contacto {contact_id}")
                        else:
                            c.execute("""
                                INSERT OR REPLACE INTO phone_mappings (bsuid, phone_number)
                                VALUES (?, ?)
                            """, (from_id, wa_id))
                            conn.commit()
                            print(f"[INFO] Mapeo guardado sin vincular a Chatwoot aún")
                        conn.close()

    return jsonify({"status": "processed"}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    init_db()
    port = int(os.getenv("PORT", "5000"))
    app.run(host='0.0.0.0', port=port)
