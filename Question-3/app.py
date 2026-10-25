
import os
import json
import hmac
import hashlib
import sqlite3
import uuid

from datetime import datetime, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "mailroom_state.db")
RECEIPT_SECRET = os.environ.get("MAILROOM_RECEIPT_SECRET", "")

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


# ============================================================
# DATABASE
# ============================================================

def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def init_db():
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS proposals (
                call_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
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
            CREATE INDEX IF NOT EXISTS idx_proposals_case
            ON proposals(case_id)
        """)


init_db()


# ============================================================
# HELPERS
# ============================================================

def canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False
    )


def digest(value):
    return hashlib.sha256(
        canonical_json(value).encode("utf-8")
    ).hexdigest()


def stable_id(prefix, *values):
    raw = "|".join(str(v) for v in values)
    return prefix + "_" + hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:24]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def error(message, status=400, code="invalid_request"):
    return jsonify({
        "error": {
            "code": code,
            "message": message
        }
    }), status


# ============================================================
# REQUEST NORMALIZATION
# ============================================================

def find_object(data, keys):
    for key in keys:
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return None


def normalize_request(data):
    """
    Accept common JSON envelope layouts without changing
    the original case data.
    """

    params = data

    if isinstance(data.get("params"), dict):
        params = data["params"]

    elif isinstance(data.get("request"), dict):
        params = data["request"]

    elif isinstance(data.get("message"), dict):
        params = data["message"]

    dossier = find_object(params, [
        "dossier",
        "case_file",
        "caseFile",
        "case",
        "payload"
    ])

    if dossier is None:
        dossier = find_object(data, [
            "dossier",
            "case_file",
            "caseFile",
            "case",
            "payload"
        ])

    if dossier is None:
        dossier = params

    call_id = (
        params.get("callId")
        or params.get("call_id")
        or params.get("id")
        or data.get("callId")
        or data.get("call_id")
    )

    if isinstance(call_id, (int, float)):
        call_id = str(call_id)

    case_id = (
        dossier.get("case_id")
        or dossier.get("caseId")
        or dossier.get("id")
        or params.get("case_id")
        or params.get("caseId")
    )

    if isinstance(case_id, (int, float)):
        case_id = str(case_id)

    operation = (
        params.get("operation")
        or params.get("phase")
        or params.get("method")
        or params.get("actionType")
        or data.get("operation")
        or data.get("phase")
        or ""
    )

    operation = str(operation).lower()

    if operation.endswith(".propose"):
        operation = "propose"

    elif operation.endswith(".commit"):
        operation = "commit"

    elif operation.endswith(".get"):
        operation = "get"

    supplied_digest = (
        params.get("inputDigest")
        or params.get("input_digest")
        or data.get("inputDigest")
        or data.get("input_digest")
    )

    return {
        "call_id": str(call_id).strip() if call_id is not None else "",
        "case_id": str(case_id).strip() if case_id is not None else "",
        "operation": operation,
        "dossier": dossier,
        "params": params,
        "supplied_digest": supplied_digest
    }


# ============================================================
# ACTION CATALOG
# ============================================================

def extract_catalog(data, normalized):
    params = normalized["params"]
    dossier = normalized["dossier"]

    for source in (params, data, dossier):
        for key in (
            "actionCatalog",
            "action_catalog",
            "availableActions",
            "available_actions",
            "actions",
            "tools"
        ):
            catalog = source.get(key)

            if isinstance(catalog, (list, dict)) and catalog:
                return catalog

    return []


def catalog_names(catalog):
    result = set()

    if isinstance(catalog, dict):
        result.update(catalog.keys())

    elif isinstance(catalog, list):
        for item in catalog:
            if isinstance(item, str):
                result.add(item)

            elif isinstance(item, dict):
                name = (
                    item.get("name")
                    or item.get("action")
                    or item.get("id")
                )

                if isinstance(name, str):
                    result.add(name)

    return result


def get_action_definition(catalog, action):
    if isinstance(catalog, dict):
        value = catalog.get(action)
        return value if isinstance(value, dict) else {}

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


def validate_arguments(catalog, action, arguments):
    if not isinstance(arguments, dict):
        return False

    definition = get_action_definition(catalog, action)

    schema = (
        definition.get("inputSchema")
        or definition.get("input_schema")
        or definition.get("parameters")
        or definition.get("arguments")
        or definition.get("args")
        or {}
    )

    if isinstance(schema, dict):
        properties = schema.get("properties", schema)
        required = schema.get("required", [])

        if isinstance(properties, dict) and properties:
            if not set(arguments).issubset(set(properties)):
                return False

        if isinstance(required, list):
            if any(key not in arguments for key in required):
                return False

    return True


# ============================================================
# EVIDENCE EXTRACTION
# ============================================================

def extract_evidence(dossier):
    """
    Preserve the source paths and original text so that
    proposals can cite actual source material.
    """

    result = []

    def walk(value, path="$"):
        if isinstance(value, str):
            for line_number, line in enumerate(
                value.splitlines(), start=1
            ):
                if line.strip():
                    result.append({
                        "source": path,
                        "line": line_number,
                        "text": line.strip()
                    })

        elif isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + "." + str(key))

        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(dossier)
    return result


def validate_evidence(evidence, available):
    if not isinstance(evidence, list) or not evidence:
        return False

    available_set = {
        (
            item["source"],
            item["line"],
            item["text"]
        )
        for item in available
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

        if (source, line, text) not in available_set:
            return False

    return True


# ============================================================
# AUTHORITY AND CASE STATE
# ============================================================

def evaluate_case(dossier):
    """
    Extract factual state. Do not authorize forwarding merely
    because a recipient or signature field exists.
    """

    authority = (
        dossier.get("approval_document")
        or dossier.get("authority")
        or dossier.get("authorization")
        or {}
    )

    if not isinstance(authority, dict):
        authority = {}

    recipient = (
        dossier.get("recipient")
        or dossier.get("intended_recipient")
    )

    authorized_recipient = (
        authority.get("recipient")
        or authority.get("approved_for")
    )

    authority_status = str(
        authority.get("status", "UNKNOWN")
    ).upper()

    case_status = str(
        dossier.get("status", "UNKNOWN")
    ).upper()

    evidence_id = (
        authority.get("evidence_id")
        or dossier.get("evidence_id")
    )

    problems = []

    if not authority:
        problems.append("missing_authority")

    if authority_status in (
        "EXPIRED",
        "REVOKED",
        "SUPERSEDED",
        "INVALID"
    ):
        problems.append("invalid_authority")

    if not evidence_id:
        problems.append("missing_provenance")

    if (
        recipient
        and authorized_recipient
        and str(recipient).casefold()
        != str(authorized_recipient).casefold()
    ):
        problems.append("recipient_mismatch")

    return {
        "recipient": recipient,
        "authorized_recipient": authorized_recipient,
        "authority_status": authority_status,
        "case_status": case_status,
        "evidence_id": evidence_id,
        "problems": problems
    }


# ============================================================
# AI PROPOSAL
# ============================================================

def ask_ai(data, dossier, catalog, source_lines, state):
    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        return None

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)

        instructions = """
You are a cautious mailroom decision agent.

You must analyze the supplied dossier and select exactly one
action from the supplied action catalog.

Rules:
- Use only exact action names in the catalog.
- Use only argument keys allowed by that action's schema.
- Never invent an ID, recipient, approval, authority document,
  receipt, or source citation.
- Consider authority, provenance, recipient matching, and
  current case state.
- An unsigned receipt is not proof of completion.
- Do not infer authorization from action words alone.
- Never perform an external side effect.
- Cite every source line necessary to support the decision.
- Do not include unrelated evidence.
- Return exactly one proposal.

Return JSON with this structure:
{
  "action": "exact catalog action",
  "arguments": {},
  "evidence": [
    {
      "source": "$.field",
      "line": 1,
      "text": "exact source line"
    }
  ],
  "reason": "short factual explanation"
}

If the dossier is ambiguous or unsafe, choose the documented
hold or reject action from the catalog when available.
Never invent a fallback action.
"""

        prompt = {
            "request": data,
            "dossier": dossier,
            "action_catalog": catalog,
            "source_lines": source_lines,
            "case_assessment": state
        }

        response = client.chat.completions.create(
            model=os.environ.get(
                "OPENAI_MODEL",
                "gpt-4.1-mini"
            ),
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": instructions
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

        content = response.choices[0].message.content

        if not content:
            return None

        result = json.loads(content)

        if not isinstance(result, dict):
            return None

        return result

    except Exception:
        app.logger.exception("Proposal generation failed")
        return None


# ============================================================
# SIGNED RECEIPTS
# ============================================================

def verify_receipt(receipt, call_id, input_digest):
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

    unsigned = {
        key: value
        for key, value in receipt.items()
        if key != "signature"
    }

    expected = hmac.new(
        RECEIPT_SECRET.encode(),
        canonical_json(unsigned).encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return None

    return {
        "receipt_id": receipt_id
    }


# ============================================================
# RESPONSE ENVELOPE
# ============================================================

def proposal_response(
    call_id,
    input_digest,
    proposal_id,
    action,
    arguments,
    evidence,
    status,
    receipt_id=None,
    reason=""
):
    return {
        "callId": call_id,
        "inputDigest": input_digest,
        "status": status,
        "receipt_id": receipt_id,
        "proposals": [
            {
                "proposal_id": proposal_id,
                "action": action,
                "arguments": arguments,
                "evidence": evidence,
                "reason": reason,
                "status": status
            }
        ]
    }


# ============================================================
# HEALTH AND AGENT CARD
# ============================================================

@app.get("/")
@app.get("/health")
def health():
    return jsonify({
        "status": "running",
        "service": "Safe AI Mailroom Agent"
    })


@app.get("/.well-known/agent-card.json")
@app.get("/a2a/.well-known/agent-card.json")
def agent_card():
    base_url = os.environ.get(
        "PUBLIC_BASE_URL",
        request.host_url.rstrip("/")
    ).rstrip("/")

    return jsonify({
        "name": "Safe AI Mailroom Agent",
        "description": (
            "Evidence-based mailroom proposals with "
            "authority checks and retry-safe state."
        ),
        "version": "1.0.0",
        "url": base_url,
        "capabilities": {
            "authority_verification": True,
            "evidence_validation": True,
            "signed_receipts": True,
            "idempotency": True
        },
        "endpoints": {
            "mailroom_actions": (
                f"{base_url}/v1/mailroom/actions"
            )
        }
    })


# ============================================================
# PROPOSE / COMMIT API
# ============================================================

@app.route(
    "/v1/mailroom/actions",
    methods=["POST", "GET"]
)
@app.route(
    "/a2a/v1/mailroom/actions",
    methods=["POST", "GET"]
)
def mailroom_actions():

    if request.method == "GET":
        with connect_db() as conn:
            rows = conn.execute("""
                SELECT call_id, case_id, input_digest,
                       proposal_id, action, status, receipt_id
                FROM proposals
                ORDER BY created_at DESC
                LIMIT 100
            """).fetchall()

        return jsonify({
            "actions": [dict(row) for row in rows]
        })

    if not request.is_json:
        return error(
            "Expected application/json",
            415,
            "invalid_content_type"
        )

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return error(
            "Request must be a JSON object",
            400,
            "malformed_request"
        )

    normalized = normalize_request(data)

    call_id = normalized["call_id"]
    case_id = normalized["case_id"]
    operation = normalized["operation"]
    dossier = normalized["dossier"]

    if not call_id:
        return error(
            "Missing callId",
            400,
            "missing_call_id"
        )

    if not case_id:
        return error(
            "Missing case ID",
            400,
            "missing_case_id"
        )

    catalog = extract_catalog(data, normalized)

    if not catalog:
        return error(
            "Missing documented action catalog",
            422,
            "missing_action_catalog"
        )

    input_data = {
        "case_id": case_id,
        "dossier": dossier,
        "action_catalog": catalog
    }

    input_digest = digest(input_data)

    supplied_digest = normalized["supplied_digest"]

    if (
        supplied_digest is not None
        and supplied_digest != input_digest
    ):
        return error(
            "inputDigest does not match the request",
            409,
            "digest_mismatch"
        )

    proposal_id = stable_id(
        "prop",
        call_id,
        input_digest
    )

    receipt = verify_receipt(
        (
            normalized["params"].get("signedReceipt")
            or normalized["params"].get("receipt")
            or data.get("signedReceipt")
            or data.get("receipt")
        ),
        call_id,
        input_digest
    )

    with connect_db() as conn:

        existing = conn.execute(
            "SELECT * FROM proposals WHERE call_id = ?",
            (call_id,)
        ).fetchone()

        # Identical retry: reuse the stored result.
        if existing:

            if (
                existing["case_id"] != case_id
                or existing["input_digest"] != input_digest
            ):
                return error(
                    "callId reused with different input",
                    409,
                    "call_id_conflict"
                )

            if operation == "commit":

                if existing["status"] == "COMPLETED":
                    return jsonify(
                        json.loads(existing["response"])
                    )

                if not receipt:
                    return error(
                        "A valid signed receipt is required",
                        403,
                        "invalid_receipt"
                    )

                conn.execute("""
                    UPDATE proposals
                    SET status = 'COMPLETED',
                        receipt_id = ?
                    WHERE call_id = ?
                """, (
                    receipt["receipt_id"],
                    call_id
                ))

                response = json.loads(existing["response"])
                response["status"] = "COMPLETED"
                response["receipt_id"] = receipt["receipt_id"]

                for item in response.get("proposals", []):
                    item["status"] = "COMPLETED"

                conn.execute("""
                    UPDATE proposals
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

        # Different call IDs cannot silently replace an
        # existing proposal for the same case.
        previous = conn.execute("""
            SELECT call_id, input_digest
            FROM proposals
            WHERE case_id = ?
            LIMIT 1
        """, (case_id,)).fetchone()

        if previous:
            return error(
                "A proposal already exists for this case",
                409,
                "case_conflict"
            )

        # A commit must not create a fresh proposal.
        if operation == "commit":
            return error(
                "No existing proposal is available to commit",
                409,
                "proposal_not_found"
            )

        source_lines = extract_evidence(dossier)
        state = evaluate_case(dossier)

        proposal = ask_ai(
            data,
            dossier,
            catalog,
            source_lines,
            state
        )

        if proposal is None:
            return error(
                "Could not safely generate a proposal",
                422,
                "decision_failed"
            )

        action = proposal.get("action")
        arguments = proposal.get("arguments")
        evidence = proposal.get("evidence")

        if not isinstance(action, str):
            return error(
                "Proposal action must be a string",
                422,
                "invalid_action"
            )

        if action not in catalog_names(catalog):
            return error(
                "Action is not in the documented catalog",
                422,
                "unsupported_action"
            )

        if not validate_arguments(
            catalog,
            action,
            arguments
        ):
            return error(
                "Action arguments do not match the schema",
                422,
                "invalid_arguments"
            )

        if not validate_evidence(
            evidence,
            source_lines
        ):
            return error(
                "Evidence citations are invalid",
                422,
                "invalid_evidence"
            )

        status = "PROPOSED"

        response = proposal_response(
            call_id=call_id,
            input_digest=input_digest,
            proposal_id=proposal_id,
            action=action,
            arguments=arguments,
            evidence=evidence,
            status=status,
            reason=str(proposal.get("reason", ""))
        )

        conn.execute("""
            INSERT INTO proposals (
                call_id, case_id, input_digest, proposal_id,
                action, arguments, evidence, status,
                response, receipt_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            call_id,
            case_id,
            input_digest,
            proposal_id,
            action,
            canonical_json(arguments),
            canonical_json(evidence),
            status,
            canonical_json(response),
            None,
            utc_now()
        ))

        return jsonify(response), 200


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(413)
def too_large(_error):
    return error(
        "Request body too large",
        413,
        "request_too_large"
    )


@app.errorhandler(500)
def server_error(_error):
    app.logger.exception("Unhandled server error")
    return error(
        "Internal server error",
        500,
        "internal_error"
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000"))
    )

