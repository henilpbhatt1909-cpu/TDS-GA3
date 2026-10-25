
import os
import json
import hmac
import hashlib
import sqlite3
from datetime import datetime, timezone

from flask import Flask, request, jsonify

app = Flask(__name__)

DB_NAME = os.environ.get("DB_PATH", "invoice_state.db")
RECEIPT_SECRET = os.environ.get("RECEIPT_SECRET", "")

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


# --------------------------------------------------
# DATABASE AND PERSISTENT STATE
# --------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                call_id TEXT PRIMARY KEY,
                invoice_id TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                action TEXT NOT NULL,
                arguments TEXT NOT NULL,
                evidence TEXT NOT NULL,
                status TEXT NOT NULL,
                response TEXT NOT NULL,
                receipt_id TEXT,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_invoice_id
            ON tasks(invoice_id)
        """)


init_db()


# --------------------------------------------------
# GENERAL HELPERS
# --------------------------------------------------

def canonical_json(data):
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False
    )


def sha256_digest(data):
    return hashlib.sha256(
        canonical_json(data).encode("utf-8")
    ).hexdigest()


def stable_id(prefix, *parts):
    value = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"{prefix}_{digest[:20]}"


def now():
    return datetime.now(timezone.utc).isoformat()


def error_response(code, message, status):
    return jsonify({
        "error": {
            "code": code,
            "message": message
        }
    }), status


# --------------------------------------------------
# REQUEST PARSING
# --------------------------------------------------

def extract_invoice(data):
    for key in (
        "invoice",
        "invoice_package",
        "invoicePackage",
        "dossier",
        "payload"
    ):
        value = data.get(key)

        if isinstance(value, dict):
            return value

    return data


def extract_invoice_id(invoice):
    for key in (
        "invoice_id",
        "invoiceId",
        "id"
    ):
        value = invoice.get(key)

        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()

    return None


def extract_action_catalog(data, invoice):
    for source in (data, invoice):
        for key in (
            "actionCatalog",
            "action_catalog",
            "availableActions",
            "available_actions",
            "actions",
            "tools"
        ):
            value = source.get(key)

            if isinstance(value, (list, dict)) and value:
                return value

    return []


def get_action_names(catalog):
    names = set()

    if isinstance(catalog, list):
        for item in catalog:
            if isinstance(item, str):
                names.add(item)

            elif isinstance(item, dict):
                name = (
                    item.get("name")
                    or item.get("action")
                    or item.get("id")
                )

                if isinstance(name, str):
                    names.add(name)

    elif isinstance(catalog, dict):
        names.update(catalog.keys())

    return names


def get_action_schema(catalog, action):
    if isinstance(catalog, dict):
        schema = catalog.get(action)

        if isinstance(schema, dict):
            return schema

    if isinstance(catalog, list):
        for item in catalog:
            if not isinstance(item, dict):
                continue

            name = (
                item.get("name")
                or item.get("action")
                or item.get("id")
            )

            if name == action:
                return item

    return {}


# --------------------------------------------------
# DOCUMENT EVIDENCE
# --------------------------------------------------

def extract_evidence_lines(value):
    lines = []

    def walk(obj, path="$"):
        if isinstance(obj, str):
            for number, line in enumerate(obj.splitlines(), start=1):
                if line.strip():
                    lines.append({
                        "source": path,
                        "line": number,
                        "text": line.strip()
                    })

        elif isinstance(obj, dict):
            for key, child in obj.items():
                walk(child, f"{path}.{key}")

        elif isinstance(obj, list):
            for index, child in enumerate(obj):
                walk(child, f"{path}[{index}]")

    walk(value)
    return lines


def validate_evidence(evidence, source_lines):
    if not isinstance(evidence, list) or not evidence:
        return False

    available = {
        (
            item["source"],
            item["line"],
            item["text"]
        )
        for item in source_lines
    }

    for item in evidence:
        if not isinstance(item, dict):
            return False

        source = item.get("source")
        line = item.get("line")
        text = item.get("text")

        if not isinstance(source, str):
            return False

        if not isinstance(line, int) or isinstance(line, bool):
            return False

        if not isinstance(text, str):
            return False

        if (source, line, text) not in available:
            return False

    return True


# --------------------------------------------------
# ACTION ARGUMENT VALIDATION
# --------------------------------------------------

def validate_arguments(catalog, action, arguments):
    if not isinstance(arguments, dict):
        return False

    schema = get_action_schema(catalog, action)

    argument_schema = {}
    required = schema.get("required", [])

    for key in (
        "arguments",
        "args",
        "parameters",
        "inputSchema",
        "input_schema"
    ):
        value = schema.get(key)

        if isinstance(value, dict):
            argument_schema = value.get(
                "properties",
                value
            )

            required = value.get("required", required)
            break

        if isinstance(value, list):
            argument_schema = {
                item.get("name") or item.get("key"): item
                for item in value
                if isinstance(item, dict)
                and (item.get("name") or item.get("key"))
            }
            break

    # If a schema defines argument keys, reject unknown keys.
    if argument_schema:
        if not set(arguments).issubset(set(argument_schema)):
            return False

    if isinstance(required, list):
        if any(key not in arguments for key in required):
            return False

    return True


# --------------------------------------------------
# SIGNED RECEIPT VALIDATION
# --------------------------------------------------

def verify_signed_receipt(receipt, call_id, input_digest):
    if not isinstance(receipt, dict):
        return None

    if not RECEIPT_SECRET:
        return None

    signature = receipt.get("signature")
    receipt_id = (
        receipt.get("receiptId")
        or receipt.get("receipt_id")
    )

    receipt_call_id = (
        receipt.get("callId")
        or receipt.get("call_id")
    )

    receipt_digest = (
        receipt.get("inputDigest")
        or receipt.get("input_digest")
    )

    status = str(receipt.get("status", "")).upper()

    if not all(
        isinstance(value, str) and value
        for value in (
            signature,
            receipt_id,
            receipt_call_id,
            receipt_digest
        )
    ):
        return None

    if receipt_call_id != call_id:
        return None

    if receipt_digest != input_digest:
        return None

    if status not in (
        "COMPLETED",
        "COMMITTED",
        "PAID",
        "RECEIVED"
    ):
        return None

    unsigned_receipt = {
        key: value
        for key, value in receipt.items()
        if key != "signature"
    }

    expected_signature = hmac.new(
        RECEIPT_SECRET.encode(),
        canonical_json(unsigned_receipt).encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(
        signature,
        expected_signature
    ):
        return None

    return {
        "receipt_id": receipt_id,
        "signature": signature
    }


# --------------------------------------------------
# AI DECISION MAKING
# --------------------------------------------------

def ask_ai(data, invoice, catalog, source_lines):
    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        return None, "OPENAI_API_KEY is not configured"

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)

        prompt = {
            "request": data,
            "invoice": invoice,
            "action_catalog": catalog,
            "evidence_lines": source_lines
        }

        result = client.chat.completions.create(
            model=os.environ.get(
                "OPENAI_MODEL",
                "gpt-4.1-mini"
            ),
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": """
You are an invoice decision agent.

Analyze the invoice package and supporting documents.

Rules:
1. Use documentary evidence, authorization and invoice state.
2. Never choose an action outside the supplied action catalog.
3. Never invent invoice IDs, payment IDs or authorization.
4. A payment for a different invoice is NOT a duplicate.
5. A payment must not be repeated on a retry.
6. Do not claim that a payment occurred without a valid receipt.
7. Return exactly one proposal.
8. Use only the documented argument keys for the chosen action.
9. Cite the exact evidence lines supporting the decision.
10. Do not perform external financial transactions.

Return JSON:
{
  "action": "exact catalog action",
  "arguments": {},
  "evidence": [
    {
      "source": "$.field",
      "line": 1,
      "text": "exact source text"
    }
  ],
  "reason": "brief explanation"
}

If no safe documented action can be selected,
return action "__NO_SAFE_ACTION__".
"""
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        prompt,
                        ensure_ascii=False,
                        default=str
                    )
                }
            ]
        )

        answer = result.choices[0].message.content

        return json.loads(answer), None

    except Exception as exc:
        return None, (
            f"AI processing failed: {type(exc).__name__}"
        )


# --------------------------------------------------
# PROPOSAL VALIDATION
# --------------------------------------------------

def validate_proposal(proposal, catalog, source_lines):
    if not isinstance(proposal, dict):
        return None

    action = proposal.get("action")
    arguments = proposal.get("arguments")
    evidence = proposal.get("evidence")

    if not isinstance(action, str) or not action:
        return None

    if action == "__NO_SAFE_ACTION__":
        return None

    if action not in get_action_names(catalog):
        return None

    if not validate_arguments(
        catalog,
        action,
        arguments
    ):
        return None

    if not validate_evidence(
        evidence,
        source_lines
    ):
        return None

    return {
        "action": action,
        "arguments": arguments,
        "evidence": evidence,
        "reason": str(proposal.get("reason", ""))
    }


# --------------------------------------------------
# HEALTH CHECK AND AGENT DISCOVERY
# --------------------------------------------------

@app.get("/")
@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "Invoice Decision Agent"
    })


@app.get("/.well-known/agent-card.json")
@app.get("/a2a/.well-known/agent-card.json")
def agent_card():
    base_url = os.environ.get(
        "PUBLIC_BASE_URL",
        request.host_url.rstrip("/")
    ).rstrip("/")

    return jsonify({
        "name": "Invoice Decision Agent",
        "description": (
            "Evidence-based invoice decisions with "
            "approval checks and retry protection."
        ),
        "version": "1.0.0",
        "url": base_url,
        "capabilities": {
            "evidence_validation": True,
            "idempotency": True,
            "signed_receipt_validation": True
        },
        "endpoints": {
            "invoice_actions": (
                f"{base_url}/v1/invoice/actions"
            )
        }
    })


# --------------------------------------------------
# MAIN API
# --------------------------------------------------

@app.route(
    "/v1/invoice/actions",
    methods=["POST", "GET"]
)
@app.route(
    "/v1/mailroom/actions",
    methods=["POST", "GET"]
)
@app.route(
    "/a2a/v1/mailroom/actions",
    methods=["POST", "GET"]
)
def invoice_actions():

    if request.method == "GET":
        with get_db() as conn:
            rows = conn.execute("""
                SELECT call_id, invoice_id, input_digest,
                       action, status, created_at
                FROM tasks
                ORDER BY created_at DESC
                LIMIT 100
            """).fetchall()

        return jsonify({
            "tasks": [dict(row) for row in rows]
        })

    if not request.is_json:
        return error_response(
            "invalid_content_type",
            "Content-Type must be application/json",
            415
        )

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return error_response(
            "malformed_request",
            "Expected a valid JSON object",
            400
        )

    call_id = (
        data.get("callId")
        or data.get("call_id")
    )

    if not isinstance(call_id, str) or not call_id.strip():
        return error_response(
            "missing_call_id",
            "A stable callId is required",
            400
        )

    call_id = call_id.strip()

    invoice = extract_invoice(data)

    invoice_id = extract_invoice_id(invoice)

    if not invoice_id:
        return error_response(
            "missing_invoice_id",
            "A real invoice ID is required",
            400
        )

    catalog = extract_action_catalog(
        data,
        invoice
    )

    if not catalog:
        return error_response(
            "missing_action_catalog",
            "No documented action catalog was supplied",
            422
        )

    input_data = {
        "invoice": invoice,
        "action_catalog": catalog
    }

    input_digest = sha256_digest(input_data)

    supplied_digest = (
        data.get("inputDigest")
        or data.get("input_digest")
    )

    if (
        supplied_digest is not None
        and supplied_digest != input_digest
    ):
        return error_response(
            "digest_mismatch",
            "inputDigest does not match request input",
            409
        )

    receipt = verify_signed_receipt(
        data.get("signedReceipt")
        or data.get("signed_receipt")
        or data.get("receipt"),
        call_id,
        input_digest
    )

    # Check existing task before considering any new action.
    with get_db() as conn:

        existing = conn.execute(
            "SELECT * FROM tasks WHERE call_id = ?",
            (call_id,)
        ).fetchone()

        if existing:

            if (
                existing["input_digest"] != input_digest
                or existing["invoice_id"] != invoice_id
            ):
                return error_response(
                    "call_id_conflict",
                    "The callId was reused with different input",
                    409
                )

            if receipt and existing["status"] != "COMPLETED":
                conn.execute("""
                    UPDATE tasks
                    SET status = 'COMPLETED',
                        receipt_id = ?
                    WHERE call_id = ?
                """, (
                    receipt["receipt_id"],
                    call_id
                ))

                response = json.loads(
                    existing["response"]
                )

                response["status"] = "COMPLETED"
                response["receipt_id"] = (
                    receipt["receipt_id"]
                )

                for proposal in response.get(
                    "proposals", []
                ):
                    proposal["status"] = "COMPLETED"

                conn.execute("""
                    UPDATE tasks
                    SET response = ?
                    WHERE call_id = ?
                """, (
                    canonical_json(response),
                    call_id
                ))

                return jsonify(response)

            return jsonify(
                json.loads(existing["response"])
            )

        # Different call IDs must not create duplicate
        # proposals for the same invoice with changed input.
        previous = conn.execute(
            "SELECT input_digest FROM tasks "
            "WHERE invoice_id = ? LIMIT 1",
            (invoice_id,)
        ).fetchone()

        if previous and previous["input_digest"] != input_digest:
            return error_response(
                "invoice_conflict",
                "This invoice already has a proposal "
                "with different input",
                409
            )

        # Do not assume a payment was made merely because
        # a receipt field or boolean is present.
        source_lines = extract_evidence_lines(
            invoice
        )

        proposal, ai_error = ask_ai(
            data,
            invoice,
            catalog,
            source_lines
        )

        if proposal is None:
            return error_response(
                "decision_failed",
                ai_error or "No safe decision available",
                422
            )

        valid = validate_proposal(
            proposal,
            catalog,
            source_lines
        )

        if valid is None:
            return error_response(
                "invalid_proposal",
                "Action, arguments, or evidence failed validation",
                422
            )

        proposal_id = stable_id(
            "proposal",
            call_id,
            input_digest
        )

        status = (
            "COMPLETED"
            if receipt
            else "PROPOSED"
        )

        response = {
            "callId": call_id,
            "inputDigest": input_digest,
            "status": status,
            "receipt_id": (
                receipt["receipt_id"]
                if receipt else None
            ),
            "proposals": [
                {
                    "proposal_id": proposal_id,
                    "action": valid["action"],
                    "arguments": valid["arguments"],
                    "evidence": valid["evidence"],
                    "reason": valid["reason"],
                    "status": status
                }
            ]
        }

        try:
            conn.execute("""
                INSERT INTO tasks (
                    call_id,
                    invoice_id,
                    input_digest,
                    action,
                    arguments,
                    evidence,
                    status,
                    response,
                    receipt_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                call_id,
                invoice_id,
                input_digest,
                valid["action"],
                canonical_json(valid["arguments"]),
                canonical_json(valid["evidence"]),
                status,
                canonical_json(response),
                receipt["receipt_id"] if receipt else None,
                now()
            ))

        except sqlite3.IntegrityError:
            existing = conn.execute(
                "SELECT * FROM tasks WHERE call_id = ?",
                (call_id,)
            ).fetchone()

            if (
                existing
                and existing["input_digest"] == input_digest
            ):
                return jsonify(
                    json.loads(existing["response"])
                )

            return error_response(
                "state_conflict",
                "Concurrent request conflict; retry "
                "with the same callId and input",
                409
            )

        return jsonify(response)


# --------------------------------------------------
# ERROR HANDLING
# --------------------------------------------------

@app.errorhandler(413)
def request_too_large(_error):
    return error_response(
        "request_too_large",
        "Request body exceeds the size limit",
        413
    )


@app.errorhandler(500)
def internal_error(_error):
    return error_response(
        "internal_error",
        "Internal server error",
        500
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000"))
    )

