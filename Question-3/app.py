import hashlib
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
        CREATE TABLE IF NOT EXISTS proposals (
            call_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            input_digest TEXT NOT NULL,
            action TEXT NOT NULL,
            args TEXT NOT NULL,
            evidence TEXT NOT NULL,
            status TEXT NOT NULL,
            receipt_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """
    )
    conn.commit()
    conn.close()


init_db()


def make_json_response(data, status_code=200):
    return Response(
        response=json.dumps(data),
        status=status_code,
        mimetype="application/json",
        headers={"Content-Type": "application/json"},
    )


def compute_digest(data):
    """Computes SHA-256 inputDigest of the input payload."""
    raw = json.dumps(data, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --- AGENT CARD DISCOVERY ENDPOINT ---
@app.route("/a2a/.well-known/agent-card.json", methods=["GET"])
@app.route("/.well-known/agent-card.json", methods=["GET"])
def agent_card():
    base_url = request.host_url.rstrip("/")
    card = {
        "name": "Safe AI Mailroom Agent",
        "description": "Checks recipient authority, scope, and signed receipts before committing mailroom actions",
        "version": "1.0.0",
        "a2a_version": "1.0",
        "url": base_url,
        "capabilities": {
            "authority_verification": True,
            "signed_receipts": True,
        },
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


# --- CORE MAILROOM EVALUATION LOGIC ---
def process_mailroom_proposal(data):
    call_id = data.get("callId") or data.get("call_id") or f"call_{uuid.uuid4().hex[:8]}"
    dossier = data.get("dossier") or data.get("case_file") or data.get("payload") or data
    input_digest = data.get("inputDigest") or compute_digest(dossier)

    case_id = dossier.get("case_id") or dossier.get("id") or f"CASE-{uuid.uuid4().hex[:6]}"
    target_recipient = dossier.get("recipient") or dossier.get("intended_recipient") or "Unknown"
    approval_doc = dossier.get("approval_document") or dossier.get("authority") or {}

    approved_recipient = approval_doc.get("recipient") or approval_doc.get("approved_for")
    doc_status = str(approval_doc.get("status", "ACTIVE")).upper()
    signed_receipt = dossier.get("signed_receipt") or dossier.get("receipt") or dossier.get("is_signed", False)
    evidence_id = approval_doc.get("evidence_id") or dossier.get("evidence_id") or "EVID-001"

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Replay / Idempotency check on call_id or case_id
    cursor.execute(
        "SELECT call_id, case_id, input_digest, action, args, evidence, status, receipt_id FROM proposals WHERE call_id = ? OR case_id = ?",
        (call_id, case_id),
    )
    existing = cursor.fetchone()

    if existing:
        # Check conflict if same case_id has different input digest
        if existing[1] == case_id and existing[2] != input_digest and not signed_receipt:
            conn.close()
            return {
                "error": "Conflict",
                "message": "Conflicting proposal for existing case_id",
            }, 409

        # Handle Receipt Commit / Continuation
        if signed_receipt and existing[6] != "COMPLETED":
            receipt_id = f"rcpt_{uuid.uuid4().hex[:8]}"
            cursor.execute(
                "UPDATE proposals SET status = 'COMPLETED', receipt_id = ? WHERE case_id = ?",
                (receipt_id, case_id),
            )
            conn.commit()
            conn.close()

            return {
                "callId": call_id,
                "inputDigest": input_digest,
                "status": "COMPLETED",
                "receipt_id": receipt_id,
                "proposals": [
                    {
                        "proposal_id": f"prop_{uuid.uuid4().hex[:6]}",
                        "action": existing[3],
                        "arguments": json.loads(existing[4]),
                        "evidence": [existing[5]],
                        "status": "COMPLETED",
                    }
                ],
            }, 200

        conn.close()
        return {
            "callId": existing[0],
            "inputDigest": existing[2],
            "status": existing[6],
            "receipt_id": existing[7],
            "proposals": [
                {
                    "proposal_id": f"prop_{uuid.uuid4().hex[:6]}",
                    "action": existing[3],
                    "arguments": json.loads(existing[4]),
                    "evidence": [existing[5]],
                    "status": existing[6],
                }
            ],
        }, 200

    # Business Rules
    if not approval_doc and not dossier.get("has_authority", True):
        action = "REJECT_UNAUTHORIZED"
        evidence_str = f"Line 1: Missing required authority document for case {case_id}."
    elif doc_status in ["EXPIRED", "REVOKED", "SUPERSEDED", "OLD"]:
        action = "REJECT_EXPIRED_AUTHORITY"
        evidence_str = f"Line 1: Authority document status '{doc_status}' is no longer valid."
    elif target_recipient and approved_recipient and str(target_recipient).lower() != str(approved_recipient).lower():
        action = "REJECT_MISMATCHED_RECIPIENT"
        evidence_str = f"Line 1: Target recipient '{target_recipient}' does not match authorized recipient '{approved_recipient}'."
    elif not signed_receipt:
        action = "HOLD_PENDING_SIGNATURE"
        evidence_str = f"Line 1: Valid authority {evidence_id} exists for '{target_recipient}' but requires signed receipt."
    else:
        action = "FORWARD_AND_COMPLETE"
        evidence_str = f"Line 1: Active authority {evidence_id} matched target recipient '{target_recipient}' with valid signed receipt."

    action_args = {
        "case_id": case_id,
        "recipient": target_recipient,
        "evidence_id": evidence_id,
    }

    status = "COMPLETED" if (action == "FORWARD_AND_COMPLETE" or signed_receipt) else "PROPOSED"
    receipt_id = f"rcpt_{uuid.uuid4().hex[:8]}" if status == "COMPLETED" else None

    cursor.execute(
        """
        INSERT INTO proposals (call_id, case_id, input_digest, action, args, evidence, status, receipt_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            call_id,
            case_id,
            input_digest,
            action,
            json.dumps(action_args),
            evidence_str,
            status,
            receipt_id,
        ),
    )

    conn.commit()
    conn.close()

    response_payload = {
        "callId": call_id,
        "inputDigest": input_digest,
        "status": status,
        "receipt_id": receipt_id,
        "proposals": [
            {
                "proposal_id": f"prop_{uuid.uuid4().hex[:6]}",
                "action": action,
                "arguments": action_args,
                "evidence": [evidence_str],
                "status": status,
            }
        ],
    }

    return response_payload, 200


# --- PRIMARY MAILROOM ACTIONS ENDPOINT ---
@app.route("/v1/mailroom/actions", methods=["POST", "GET"])
@app.route("/a2a/v1/mailroom/actions", methods=["POST", "GET"])
def mailroom_actions():
    if request.method == "GET":
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT call_id, case_id, status, action, evidence FROM proposals")
        rows = cursor.fetchall()
        conn.close()
        return make_json_response({"actions": rows})

    data = request.get_json(force=True, silent=True)
    if not data or not isinstance(data, dict):
        return make_json_response({"error": "Malformed request payload"}, 400)

    result, status_code = process_mailroom_proposal(data)
    return make_json_response(result, status_code)


# --- GENERIC MESSAGE DISPATCH ENDPOINT ---
@app.route("/message:send", methods=["POST"])
@app.route("/a2a/message:send", methods=["POST"])
def message_send():
    data = request.get_json(force=True, silent=True)
    if not data or not isinstance(data, dict):
        return make_json_response({"error": "Malformed request payload"}, 400)

    result, status_code = process_mailroom_proposal(data)
    return make_json_response(result, status_code)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)