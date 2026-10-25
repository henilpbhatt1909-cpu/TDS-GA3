import sqlite3
import uuid
from flask import Flask, Response, json, request

app = Flask(__name__)
DB_NAME = "mailroom_state.db"


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS mail_actions (
            action_id TEXT PRIMARY KEY,
            case_id TEXT UNIQUE,
            recipient TEXT NOT NULL,
            status TEXT NOT NULL,
            action_taken TEXT NOT NULL,
            evidence TEXT NOT NULL,
            receipt_signed INTEGER DEFAULT 0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """
    )
    conn.commit()
    conn.close()


init_db()


def make_json_response(data, status_code=200):
    """Utility to return JSON response with both standard application/json and application/a2a+json headers."""
    return Response(
        response=json.dumps(data),
        status=status_code,
        mimetype="application/json",
        headers={"Content-Type": "application/json"},
    )


# --- AGENT CARD DISCOVERY ENDPOINT ---
@app.route("/a2a/.well-known/agent-card.json", methods=["GET"])
@app.route("/.well-known/agent-card.json", methods=["GET"])
def agent_card():
    base_url = request.host_url.rstrip("/")
    card = {
        "name": "Safe Mailroom Agent",
        "description": "Checks recipient authority and signed receipts before completing mailroom actions",
        "version": "1.0.0",
        "a2a_version": "1.0",
        "url": base_url,
        "capabilities": {
            "authority_verification": True,
            "signed_receipts": True,
        },
        "skills": [
            {
                "id": "mailroom-handler",
                "name": "Mailroom Authority Processor",
                "description": "Verifies scope, recipient authority, and signed receipt verification",
                "modes": ["sync", "async"],
            }
        ],
        "endpoints": {
            "mailroom_actions": f"{base_url}/v1/mailroom/actions",
            "message_send": f"{base_url}/message:send",
        },
    }
    return make_json_response(card)


# --- HEALTH CHECK / ROOT ---
@app.route("/", methods=["GET", "HEAD"])
@app.route("/v1/mailroom", methods=["GET", "HEAD"])
def root():
    return make_json_response({"status": "running", "service": "Safe AI Mailroom Agent"})


# --- CORE MAILROOM AUTHORITY LOGIC ---
def evaluate_mailroom_case(data):
    case = data.get("payload", data) if isinstance(data, dict) else {}
    case_file = case.get("case_file", case)

    case_id = case_file.get("case_id") or case_file.get("id") or data.get("case_id") or f"CASE-{uuid.uuid4().hex[:6]}"
    target_recipient = case_file.get("recipient") or case_file.get("intended_recipient") or "Default Recipient"
    approval_doc = case_file.get("approval_document", {}) or case_file.get("approval", {})

    approved_recipient = approval_doc.get("recipient") or approval_doc.get("approved_for")
    doc_status = str(approval_doc.get("status", "ACTIVE")).upper()
    is_signed = case_file.get("is_signed", False) or approval_doc.get("signed", False)
    evidence_id = approval_doc.get("evidence_id") or case_file.get("evidence_id", "EVID-DOC-001")

    # Idempotency / Deduplication Check
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT action_id, status, action_taken, evidence, receipt_signed FROM mail_actions WHERE case_id = ?",
        (case_id,),
    )
    existing = cursor.fetchone()

    if existing:
        conn.close()
        return {
            "action_id": existing[0],
            "case_id": case_id,
            "status": existing[1],
            "action": existing[2],
            "action_taken": existing[2],
            "evidence": existing[3],
            "evidence_cited": existing[3],
            "evidence_ids": [evidence_id],
            "receipt_signed": bool(existing[4]),
            "message": "Case retrieved from persistent state.",
        }

    # Authority and Scope Validation Rules
    if not approval_doc and not case_file.get("has_authority", True):
        action = "REJECT_UNAUTHORIZED"
        evidence = f"Case {case_id} rejected: Missing required approval document authority."
    elif doc_status in ["EXPIRED", "REVOKED", "SUPERSEDED", "OLD"]:
        action = "REJECT_EXPIRED_AUTHORITY"
        evidence = f"Case {case_id} rejected: Approval document status is '{doc_status}'."
    elif target_recipient and approved_recipient and str(target_recipient).lower() != str(approved_recipient).lower():
        action = "REJECT_MISMATCHED_RECIPIENT"
        evidence = f"Case {case_id} rejected: Approval for '{approved_recipient}' does not match target recipient '{target_recipient}'."
    elif not is_signed:
        action = "HOLD_PENDING_SIGNATURE"
        evidence = f"Case {case_id} held: Requires signed receipt before recording action as completed."
    else:
        action = "FORWARD_AND_COMPLETE"
        evidence = f"Case {case_id} completed: Valid active authorization {evidence_id} matches recipient '{target_recipient}' with signed receipt."

    action_id = f"act_{uuid.uuid4().hex[:8]}"
    status = "COMPLETED" if action == "FORWARD_AND_COMPLETE" else "PROPOSED"

    cursor.execute(
        """
        INSERT INTO mail_actions (action_id, case_id, recipient, status, action_taken, evidence, receipt_signed)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
        (
            action_id,
            case_id,
            str(target_recipient),
            status,
            action,
            evidence,
            1 if is_signed else 0,
        ),
    )

    conn.commit()
    conn.close()

    return {
        "action_id": action_id,
        "case_id": case_id,
        "status": status,
        "action": action,
        "action_taken": action,
        "evidence": evidence,
        "evidence_cited": evidence,
        "evidence_ids": [evidence_id],
        "receipt_signed": is_signed,
    }


# --- PRIMARY MAILROOM ACTIONS ENDPOINT ---
@app.route("/v1/mailroom/actions", methods=["POST", "GET"])
@app.route("/a2a/v1/mailroom/actions", methods=["POST", "GET"])
def mailroom_actions():
    if request.method == "GET":
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT action_id, case_id, status, action_taken, evidence FROM mail_actions")
        rows = cursor.fetchall()
        conn.close()
        actions = [
            {"action_id": r[0], "case_id": r[1], "status": r[2], "action": r[3], "evidence": r[4]}
            for r in rows
        ]
        return make_json_response({"actions": actions})

    data = request.get_json(force=True, silent=True) or {}
    result = evaluate_mailroom_case(data)
    
    # Return directly inside proposal envelope
    return make_json_response({
        "status": "success",
        "action_id": result["action_id"],
        "case_id": result["case_id"],
        "action": result["action"],
        "evidence": result["evidence"],
        "evidence_cited": result["evidence_cited"],
        "evidence_ids": result["evidence_ids"],
        "result": result
    }, 200)


# --- GENERIC MESSAGE DISPATCH ENDPOINT ---
@app.route("/message:send", methods=["POST"])
@app.route("/a2a/message:send", methods=["POST"])
def message_send():
    data = request.get_json(force=True, silent=True) or {}
    result = evaluate_mailroom_case(data)
    return make_json_response({"status": "success", "result": result}, 200)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)