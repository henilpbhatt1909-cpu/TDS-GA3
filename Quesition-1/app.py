import sqlite3
from flask import Flask, jsonify, request

app = Flask(__name__)
DB_NAME = "invoice_state.db"


def init_db():
    """Initializes the persistent SQLite database table for idempotency."""
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


@app.route("/a2a/process-invoice", methods=["POST"])
def process_invoice():
    data = request.get_json() or {}

    invoice_id = data.get("invoice_id")
    amount = data.get("amount", 0.0)
    has_po_match = data.get("has_po_match", False)

    if not invoice_id:
        return jsonify({"error": "Missing invoice_id"}), 400

    # 1. Idempotency Check: Prevent duplicate processing/payment
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, action_taken, evidence FROM invoice_tasks WHERE invoice_id = ?",
        (invoice_id,),
    )
    existing = cursor.fetchone()

    if existing:
        conn.close()
        return (
            jsonify(
                {
                    "invoice_id": invoice_id,
                    "status": existing[0],
                    "action_taken": existing[1],
                    "evidence_cited": existing[2],
                    "message": "Task state retrieved from store (duplicate request prevented).",
                }
            ),
            200,
        )

    # 2. Evidence & Approval Threshold Logic
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

    # 3. Store State in Database Before Responding
    cursor.execute(
        """
        INSERT INTO invoice_tasks (invoice_id, status, action_taken, evidence)
        VALUES (?, ?, ?, ?)
    """,
        (invoice_id, "COMPLETED", action, evidence),
    )

    conn.commit()
    conn.close()

    return (
        jsonify(
            {
                "invoice_id": invoice_id,
                "status": "COMPLETED",
                "action_taken": action,
                "evidence_cited": evidence,
            }
        ),
        200,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)