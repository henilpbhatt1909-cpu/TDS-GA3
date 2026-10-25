import sqlite3
from flask import Flask, Response, json, request

app = Flask(__name__)
DB_NAME = "invoice_state.db"


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS invoice_tasks (
            invoice_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            action_taken TEXT NOT NULL,
            evidence TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """
    )
    conn.commit()
    conn.close()


init_db()


def a2a_response(data, status_code=200):
    """Helper function to return JSON with the required A2A media type."""
    return Response(
        response=json.dumps(data),
        status=status_code,
        mimetype="application/a2a+json",
        headers={"Content-Type": "application/a2a+json"},
    )


# --- A2A Agent Card Endpoint ---
@app.route("/a2a/.well-known/agent-card.json", methods=["GET"])
@app.route("/.well-known/agent-card.json", methods=["GET"])
def agent_card():
    card_data = {
        "name": "Invoice Agent",
        "description": "A2A compliant invoice agent",
        "version": "1.0.0",
        "a2a_version": "1.0",
        "capabilities": ["message:send"],
        "endpoints": {"message_send": "/message:send"},
    }
    return a2a_response(card_data)


# --- Root / Health Check Routes ---
@app.route("/", methods=["GET", "HEAD"])
@app.route("/a2a/", methods=["GET", "HEAD"])
def root():
    return a2a_response({"status": "running"})


# --- Core Logic Handler ---
def process_invoice_logic(data):
    invoice_data = data.get("payload", data) if isinstance(data, dict) else {}

    invoice_id = invoice_data.get("invoice_id") or data.get("invoice_id")
    amount = float(invoice_data.get("amount", data.get("amount", 0.0)))
    has_po_match = invoice_data.get("has_po_match", data.get("has_po_match", False))

    if not invoice_id:
        invoice_id = data.get("id", "INV-UNKNOWN")

    # Idempotency Check
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, action_taken, evidence FROM invoice_tasks WHERE invoice_id = ?",
        (invoice_id,),
    )
    existing = cursor.fetchone()

    if existing:
        conn.close()
        return {
            "invoice_id": invoice_id,
            "status": existing[0],
            "action_taken": existing[1],
            "evidence_cited": existing[2],
            "message": "Retrieved from store (duplicate request blocked).",
        }

    # Business Rules Evaluation
    APPROVAL_LIMIT = 5000.0

    if not has_po_match:
        action = "REJECT"
        evidence = "Invoice rejected: No matching Purchase Order found."
    elif amount > APPROVAL_LIMIT:
        action = "FLAG_FOR_APPROVAL"
        evidence = f"Invoice flagged: Amount ${amount:.2f} exceeds threshold limit of ${APPROVAL_LIMIT:.2f}."
    else:
        action = "PAY_INVOICE"
        evidence = f"Invoice approved: Matched PO and amount ${amount:.2f} within limit."

    # Save state
    cursor.execute(
        """
        INSERT INTO invoice_tasks (invoice_id, status, action_taken, evidence)
        VALUES (?, ?, ?, ?)
    """,
        (invoice_id, "COMPLETED", action, evidence),
    )
    conn.commit()
    conn.close()

    return {
        "invoice_id": invoice_id,
        "status": "COMPLETED",
        "action_taken": action,
        "evidence_cited": evidence,
    }


# --- Standard A2A Message Handler Endpoint ---
@app.route("/a2a/message:send", methods=["POST"])
@app.route("/message:send", methods=["POST"])
def a2a_message_send():
    data = request.get_json(force=True, silent=True) or {}
    result = process_invoice_logic(data)
    return a2a_response({"result": result, "status": "success"})


# --- Legacy / Generic Fallback Route ---
@app.route("/a2a/process-invoice", methods=["POST"])
@app.route("/process-invoice", methods=["POST"])
def process_invoice():
    data = request.get_json(force=True, silent=True) or {}
    result = process_invoice_logic(data)
    return a2a_response(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)