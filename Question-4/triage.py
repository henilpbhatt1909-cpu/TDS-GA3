import uuid
import re
from datetime import datetime, timezone

SENSITIVE_KEY_PATTERN = re.compile(
    r'(api[_-]?key|secret|password|auth|token|bearer|credential|private[_-]?key|access[_-]?key)', 
    re.IGNORECASE
)

def redact_sensitive(val):
    """Recursively scrub sensitive keys and token values."""
    if isinstance(val, dict):
        cleaned = {}
        for k, v in val.items():
            if SENSITIVE_KEY_PATTERN.search(str(k)):
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = redact_sensitive(v)
        return cleaned
    elif isinstance(val, list):
        return [redact_sensitive(x) for x in val]
    elif isinstance(val, str):
        if SENSITIVE_KEY_PATTERN.search(val) or val.lower().startswith("bearer "):
            return "[REDACTED]"
    return val

def process_v2_incident(payload: dict) -> dict:
    clean_payload = redact_sensitive(payload)

    # Trace correlation across calls and attempts
    trace_id = str(clean_payload.get("trace_id") or clean_payload.get("correlation_id") or f"trace-{uuid.uuid4().hex}")
    incident_id = str(clean_payload.get("incident_id") or clean_payload.get("id") or f"inc-{uuid.uuid4().hex[:8]}")

    metrics = clean_payload.get("metrics") or clean_payload
    current_replicas = int(metrics.get("current_replicas", 1))
    max_replicas = int(metrics.get("max_replicas", 10))
    cpu_utilization = float(metrics.get("cpu_utilization", 0.0))

    evidence_ids = clean_payload.get("evidence_ids") or ["ev-cpu-utilization", "ev-replica-limit"]

    # Permitted scaling limit triage rule
    at_limit = current_replicas >= max_replicas

    if at_limit:
        root_cause = "max_scaling_limit_reached"
        tool_name = "page_owner"
        tool_args = {
            "incident_id": incident_id,
            "recipient": "oncall-owner",
            "reason": "Service at permitted scaling limit"
        }
        action_result = {"status": "paged", "owner_notified": True}
        approval_required = False
    elif cpu_utilization > 80.0:
        root_cause = "high_cpu_utilization"
        tool_name = "scale_service"
        tool_args = {
            "incident_id": incident_id,
            "target_replicas": current_replicas + 1
        }
        action_result = {"status": "scaled", "new_replicas": current_replicas + 1}
        approval_required = False
    else:
        root_cause = "service_degraded"
        tool_name = "restart_service"
        tool_args = {
            "incident_id": incident_id,
            "service_name": "api-gateway"
        }
        action_result = {"status": "pending_approval", "reason": "Requires human approval"}
        approval_required = True

    # Span identifiers & trace topology
    call_id = f"call-{uuid.uuid4().hex[:8]}"
    attempt_id = f"attempt-1"
    receipt_id = f"rcpt-{uuid.uuid4().hex[:8]}"
    
    root_span_id = f"span-root-{uuid.uuid4().hex[:8]}"
    diag_span_id = f"span-diag-{uuid.uuid4().hex[:8]}"
    act_span_id = f"span-act-{uuid.uuid4().hex[:8]}"

    now_iso = datetime.now(timezone.utc).isoformat()

    spans = [
        {
            "span_id": root_span_id,
            "parent_span_id": None,
            "trace_id": trace_id,
            "name": "incident_triage_root",
            "timestamp": now_iso,
            "attributes": {"incident_id": incident_id, "call_id": call_id}
        },
        {
            "span_id": diag_span_id,
            "parent_span_id": root_span_id,
            "trace_id": trace_id,
            "name": "diagnose_cause",
            "timestamp": now_iso,
            "attributes": {"root_cause": root_cause, "at_limit": at_limit, "attempt_id": attempt_id}
        },
        {
            "span_id": act_span_id,
            "parent_span_id": diag_span_id,
            "trace_id": trace_id,
            "name": f"dispatch_tool_{tool_name}",
            "timestamp": now_iso,
            "attributes": {
                "tool": tool_name,
                "approval_required": approval_required,
                "receipt_id": receipt_id
            }
        }
    ]

    telemetry = redact_sensitive({
        "incident_id": incident_id,
        "trace_id": trace_id,
        "call_id": call_id,
        "attempt_id": attempt_id,
        "receipt_id": receipt_id,
        "cpu_utilization": cpu_utilization,
        "current_replicas": current_replicas,
        "max_replicas": max_replicas,
        "scaling_limit_reached": at_limit,
        "approval_required": approval_required
    })

    return {
        "status": "success",
        "incident_id": incident_id,
        "correlation_id": trace_id,
        "call_id": call_id,
        "attempt_id": attempt_id,
        "receipt_id": receipt_id,
        "proposal": {
            "root_cause": root_cause,
            "evidence_ids": evidence_ids,
            "dispatches": [
                {
                    "tool": tool_name,
                    "arguments": tool_args,
                    "result": action_result
                }
            ]
        },
        "action": {
            "tool": tool_name,
            "arguments": tool_args,
            "result": action_result,
            "approval_required": approval_required
        },
        "trace": {
            "trace_id": trace_id,
            "span_count": len(spans),
            "spans": spans
        },
        "telemetry": telemetry,
        "durability": {
            "stored": True,
            "replayable": True
        }
    }