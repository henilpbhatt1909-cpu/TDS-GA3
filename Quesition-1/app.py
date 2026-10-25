import uuid
import sqlite3
from flask import Flask, Response, json, request

app = Flask(__name__)
DB_NAME = "invoice_state.db"


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Task store
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            invoice_id TEXT,
            status TEXT NOT NULL,
            action_taken TEXT NOT NULL,
            evidence TEXT NOT NULL,
            receipt_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """
    )
    # Receipts store
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS receipts (
            receipt_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """
    )
    conn.commit()
    conn.close()


init_db()


def a2a_response(data, status_code=200):
    """Returns JSON with the mandatory application/a2a+json header."""
    return Response(
        response=json.dumps(data),
        status=status_code,
        mimetype="application/a2a+json",
        headers={"Content-Type": "application/a2a+json"},
    )


def validate_a2a_request():
    """Validates authentication, A2A version, and media types."""
    # 1. Bearer Token Auth Check
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return a2a_response(
            {"error": "Unauthorized", "message": "Missing Bearer token"}, 401
        )

    # 2. Protocol Version Check
    version = request.headers.get("A2A-Version", "1.0")
    if version != "1.0":
        return a2a_response(
            {"error": "Bad Request", "message": "Unsupported A2A-Version"}, 400
        )

    # 3. Media Type Check for POST
    if request.method == "POST":
        content_type = request.headers.get("Content-Type", "")
        if "application/a2a+json" not in content_type and "application/json" not in content_type:
            return a2a_response(
                {"error": "Unsupported Media Type", "message": "Content-Type must be application/a2a+json"}, 415
            )

    return None


# --- Agent Card Endpoint ---
@app.route("/a2a/.well-known/agent-card.json", methods=["GET"])
@app.route("/.well-known/agent-card.json", methods=["GET"])
def agent_card():
    card_data = {
        "name": "A2A Invoice Agent",
        "description": "Evidence-based invoice agent supporting safety and idempotency",
        "version": "1.0.0",
        "a2a_version": "1.0",
        "capabilities": {
            "streaming": False,
            "push_notifications": False
        },
        "skills": [
            {
                "id": "invoice-processor",
                "name": "Invoice Processing Skill",
                "description": "Evaluates invoices, checks approval limits, prevents duplicate payments",
                "modes": ["sync"]
            }
        ],
        "endpoints": {
            "message_send": "/message:send",
            "tasks": "/tasks",
            "receipts": "/receipts"
        }
    }
    return a2a_response(card_data)


# --- Root / Health Check ---
@app.route("/", methods=["GET", "HEAD"])
@app.route("/a2a/", methods=["GET", "HEAD"])
def root():
    return a2a_response({"status": "running", "service": "A2A Invoice Agent"})


# --- Message Processing Endpoint ---
@app.route("/a2a/message:send", methods=["POST"])
@app.route("/message:send", methods=["POST"])
def message_send():
    err = validate_a2a_request()
    if err:
        return err

    data = request.get_json(force=True, silent=True) or {}
    payload = data.get("payload", data)

    invoice_id = payload.get("invoice_id") or data.get("invoice_id", f"INV-{uuid.uuid4().hex[:6]}")
    amount = float(payload.get("amount", data.get("amount", 0.0)))
    has_po = payload.get("has_po_match", data.get("has_po_match", False))

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Idempotency Check on invoice_id
    cursor.execute("SELECT task_id, status, action_taken, evidence, receipt_id FROM tasks WHERE invoice_id = ?", (invoice_id,))
    existing = cursor.fetchone()

    if existing:
        conn.close()
        return a2a_response({
            "task_id": existing[0],
            "status": existing[1],
            "action_taken": existing[2],
            "evidence_cited": existing[3],
            "receipt_id": existing[4],
            "message": "Existing task record retrieved."
        }, 200)

    # Business Rules
    APPROVAL_LIMIT = 5000.0
    if not has_po:
        action = "REJECT"
        evidence = "Invoice rejected: No matching Purchase Order found."
    elif amount > APPROVAL_LIMIT:
        action = "FLAG_FOR_APPROVAL"
        evidence = f"Invoice flagged: Amount ${amount:.2f} exceeds limit ${APPROVAL_LIMIT:.2f}."
    else:
        action = "PAY_INVOICE"
        evidence = f"Invoice approved: Matched PO and amount ${amount:.2f} within limit."

    task_id = f"task_{uuid.uuid4().hex[:8]}"
    receipt_id = f"rcpt_{uuid.uuid4().hex[:8]}" if action == "PAY_INVOICE" else None

    # Save Task State
    cursor.execute(
        "INSERT INTO tasks (task_id, invoice_id, status, action_taken, evidence, receipt_id) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, invoice_id, "COMPLETED", action, evidence, receipt_id)
    )

    if receipt_id:
        cursor.execute(
            "INSERT INTO receipts (receipt_id, task_id, action, details) VALUES (?, ?, ?, ?)",
            (receipt_id, task_id, action, f"Paid amount ${amount:.2f} for {invoice_id}")
        )

    conn.commit()
    conn.close()

    return a2a_response({
        "task_id": task_id,
        "invoice_id": invoice_id,
        "status": "COMPLETED",
        "action_taken": action,
        "evidence_cited": evidence,
        "receipt_id": receipt_id
    }, 200)


# --- Task Lifecycle Endpoints ---
@app.route("/a2a/tasks", methods=["GET"])
@app.route("/tasks", methods=["GET"])
def list_tasks():
    err = validate_a2a_request()
    if err:
        return err

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT task_id, invoice_id, status, action_taken, evidence, receipt_id FROM tasks")
    rows = cursor.fetchall()
    conn.close()

    tasks = [
        {
            "task_id": r[0],
            "invoice_id": r[1],
            "status": r[2],
            "action_taken": r[3],
            "evidence_cited": r[4],
            "receipt_id": r[5]
        }
        for r in rows
    ]
    return a2a_response({"tasks": tasks})


@app.route("/a2a/tasks/<task_id>", methods=["GET"])
@app.route("/tasks/<task_id>", methods=["GET"])
def get_task(task_id):
    err = validate_a2a_request()
    if err:
        return err

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT task_id, invoice_id, status, action_taken, evidence, receipt_id FROM tasks WHERE task_id = ?", (task_id,))
    r = cursor.fetchone()
    conn.close()

    if not r:
        return a2a_response({"error": "Task not found"}, 404)

    return a2a_response({
        "task_id": r[0],
        "invoice_id": r[1],
        "status": r[2],
        "action_taken": r[3],
        "evidence_cited": r[4],
        "receipt_id": r[5]
    })


# --- Receipts Endpoint ---
@app.route("/a2a/receipts", methods=["GET"])
@app.route("/receipts", methods=["GET"])
def list_receipts():
    err = validate_a2a_request()
    if err:
        return err

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT receipt_id, task_id, action, details, timestamp FROM receipts")
    rows = cursor.fetchall()
    conn.close()

    receipts = [
        {
            "receipt_id": r[0],
            "task_id": r[1],
            "action": r[2],
            "details": r[3],
            "timestamp": r[4]
        }
        for r in rows
    ]
    return a2a_response({"receipts": receipts})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)