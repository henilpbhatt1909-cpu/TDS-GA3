import sqlite3
import uuid
from flask import Flask, Response, json, request

app = Flask(__name__)
DB_NAME = "invoice_state.db"


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            invoice_id TEXT UNIQUE,
            status TEXT NOT NULL,
            action TEXT,
            evidence TEXT,
            evidence_ids TEXT,
            receipt_id TEXT,
            payload TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """
    )
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
    """Returns JSON with mandatory application/a2a+json Content-Type."""
    return Response(
        response=json.dumps(data),
        status=status_code,
        mimetype="application/a2a+json",
        headers={"Content-Type": "application/a2a+json"},
    )


def validate_a2a_request():
    """Validates authentication, versioning, and strict media types."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return a2a_response(
            {"error": "Unauthorized", "message": "Missing Bearer token"}, 401
        )

    version = request.headers.get("A2A-Version", "1.0")
    if version != "1.0":
        return a2a_response(
            {"error": "Bad Request", "message": "Unsupported A2A version"}, 400
        )

    if request.method == "POST":
        content_type = request.headers.get("Content-Type", "")
        if "application/a2a+json" not in content_type:
            return a2a_response(
                {
                    "error": "Unsupported Media Type",
                    "message": "Content-Type must be application/a2a+json",
                },
                415,
            )

    return None


# --- AGENT CARD ENDPOINT ---
@app.route("/a2a/.well-known/agent-card.json", methods=["GET"])
@app.route("/.well-known/agent-card.json", methods=["GET"])
def agent_card():
    base_url = "https://a2a-invoice-agent-kx8c.onrender.com/a2a"

    card = {
        "name": "A2A Invoice Agent",
        "description": "Evidence-based invoice agent supporting safety, idempotency, and task lifecycle",
        "version": "1.0.0",
        "a2a_version": "1.0",
        "url": base_url,
        "capabilities": {
            "streaming": False,
            "push_notifications": False,
            "state_persistence": True,
        },
        "skills": [
            {
                "id": "invoice-processing",
                "name": "Invoice Processing",
                "description": "Decide and act safely on invoices using supporting evidence",
                "modes": ["sync", "async"],
            }
        ],
        "endpoints": {
            "message_send": f"{base_url}/message:send",
            "tasks": f"{base_url}/tasks",
            "receipts": f"{base_url}/receipts",
        },
    }
    return a2a_response(card)


# --- HEALTH / ROOT ENDPOINTS ---
@app.route("/", methods=["GET", "HEAD"])
@app.route("/a2a/", methods=["GET", "HEAD"])
def root():
    return a2a_response({"status": "running", "service": "A2A Invoice Agent"})


# --- BUSINESS LOGIC EVALUATOR ---
def process_invoice_data(doc):
    invoice_id = doc.get("invoice_id") or doc.get("id") or f"INV-{uuid.uuid4().hex[:6]}"
    amount = float(doc.get("amount", doc.get("total", 0.0)))
    has_po = doc.get("has_po_match", doc.get("po_matched", False))
    evidence_id = doc.get("evidence_id") or doc.get("po_number", "EVID-001")

    APPROVAL_LIMIT = 5000.0

    if not has_po:
        action = "REJECT"
        evidence = f"Invoice {invoice_id} rejected: No matching Purchase Order found."
    elif amount > APPROVAL_LIMIT:
        action = "FLAG_FOR_APPROVAL"
        evidence = f"Invoice {invoice_id} flagged: Amount ${amount:.2f} exceeds threshold limit of ${APPROVAL_LIMIT:.2f}."
    else:
        action = "PAY_INVOICE"
        evidence = f"Invoice {invoice_id} approved: Supporting evidence {evidence_id} matches PO and amount ${amount:.2f} is within limit."

    return invoice_id, amount, action, evidence, evidence_id


# --- STEP 1: INITIAL MESSAGE SEND (Creates input-required Task) ---
@app.route("/a2a/message:send", methods=["POST"])
@app.route("/message:send", methods=["POST"])
def message_send():
    err = validate_a2a_request()
    if err:
        return err

    data = request.get_json(force=True, silent=True) or {}
    payload = data.get("payload", data)
    doc = payload.get("invoice", payload.get("document", payload))

    invoice_id = doc.get("invoice_id") or doc.get("id") or f"INV-{uuid.uuid4().hex[:6]}"

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Idempotency Check
    cursor.execute(
        "SELECT task_id, status, action, evidence, evidence_ids, receipt_id FROM tasks WHERE invoice_id = ?",
        (invoice_id,),
    )
    existing = cursor.fetchone()

    if existing:
        conn.close()
        return a2a_response(
            {
                "task_id": existing[0],
                "status": existing[1],
                "action": existing[2],
                "action_taken": existing[2],
                "evidence": existing[3],
                "evidence_cited": existing[3],
                "evidence_ids": [existing[4]] if existing[4] else [],
                "receipt_id": existing[5],
                "message": "Task state retrieved from persistent store.",
            },
            200,
        )

    task_id = f"task_{uuid.uuid4().hex[:8]}"

    # Save initial input-required task state
    cursor.execute(
        """
        INSERT INTO tasks (task_id, invoice_id, status, payload)
        VALUES (?, ?, ?, ?)
    """,
        (task_id, invoice_id, "input-required", json.dumps(doc)),
    )
    conn.commit()
    conn.close()

    return a2a_response(
        {
            "task_id": task_id,
            "invoice_id": invoice_id,
            "status": "input-required",
            "message": "Task initialized. Provide continuation evidence payload to finalize decision.",
        },
        200,
    )


# --- STEP 2: TASK CONTINUATION AND LIFECYCLE MANAGEMENT ---
@app.route("/a2a/tasks/<task_id>", methods=["GET", "POST", "DELETE"])
@app.route("/tasks/<task_id>", methods=["GET", "POST", "DELETE"])
def handle_task(task_id):
    err = validate_a2a_request()
    if err:
        return err

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Handle Cancellation / Delete
    if request.method == "DELETE" or request.path.endswith("/cancel"):
        cursor.execute(
            "UPDATE tasks SET status = 'CANCELLED' WHERE task_id = ?",
            (task_id,),
        )
        conn.commit()
        conn.close()
        return a2a_response(
            {"task_id": task_id, "status": "CANCELLED", "action": "CANCELLED"}
        )

    # Fetch Existing Task
    cursor.execute(
        "SELECT task_id, invoice_id, status, action, evidence, evidence_ids, receipt_id, payload FROM tasks WHERE task_id = ?",
        (task_id,),
    )
    row = cursor.fetchone()

    if not row:
        conn.close()
        return a2a_response({"error": "Task not found"}, 404)

    # Continuation POST request (Transition input-required -> COMPLETED)
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        continuation = data.get("payload", data)

        stored_payload = json.loads(row[7]) if row[7] else {}
        combined_doc = {**stored_payload, **continuation}

        inv_id, amount, action, evidence, evidence_id = process_invoice_data(combined_doc)
        receipt_id = f"rcpt_{uuid.uuid4().hex[:8]}" if action == "PAY_INVOICE" else None

        cursor.execute(
            """
            UPDATE tasks
            SET status = 'COMPLETED', action = ?, evidence = ?, evidence_ids = ?, receipt_id = ?
            WHERE task_id = ?
        """,
            (action, evidence, evidence_id, receipt_id, task_id),
        )

        if receipt_id:
            cursor.execute(
                """
                INSERT INTO receipts (receipt_id, task_id, action, details)
                VALUES (?, ?, ?, ?)
            """,
                (
                    receipt_id,
                    task_id,
                    action,
                    f"Paid ${amount:.2f} for {inv_id} bound to evidence {evidence_id}",
                ),
            )

        conn.commit()
        conn.close()

        return a2a_response(
            {
                "task_id": task_id,
                "invoice_id": inv_id,
                "status": "COMPLETED",
                "action": action,
                "action_taken": action,
                "evidence": evidence,
                "evidence_cited": evidence,
                "evidence_ids": [evidence_id],
                "receipt_id": receipt_id,
            },
            200,
        )

    conn.close()
    return a2a_response(
        {
            "task_id": row[0],
            "invoice_id": row[1],
            "status": row[2],
            "action": row[3],
            "action_taken": row[3],
            "evidence": row[4],
            "evidence_cited": row[4],
            "evidence_ids": [row[5]] if row[5] else [],
            "receipt_id": row[6],
        }
    )


# --- TASK LISTING ENDPOINT ---
@app.route("/a2a/tasks", methods=["GET"])
@app.route("/tasks", methods=["GET"])
def list_tasks():
    err = validate_a2a_request()
    if err:
        return err

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT task_id, invoice_id, status, action, evidence, receipt_id FROM tasks"
    )
    rows = cursor.fetchall()
    conn.close()

    tasks = [
        {
            "task_id": r[0],
            "invoice_id": r[1],
            "status": r[2],
            "action": r[3],
            "evidence": r[4],
            "receipt_id": r[5],
        }
        for r in rows
    ]
    return a2a_response({"tasks": tasks})


# --- RECEIPTS ENDPOINT ---
@app.route("/a2a/receipts", methods=["GET"])
@app.route("/receipts", methods=["GET"])
def list_receipts():
    err = validate_a2a_request()
    if err:
        return err

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT receipt_id, task_id, action, details, timestamp FROM receipts"
    )
    rows = cursor.fetchall()
    conn.close()

    receipts = [
        {
            "receipt_id": r[0],
            "task_id": r[1],
            "action": r[2],
            "details": r[3],
            "timestamp": r[4],
        }
        for r in rows
    ]
    return a2a_response({"receipts": receipts})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)