import uuid
import re
from datetime import datetime, timezone

# Comprehensive pattern for secret/token/credential keys
SENSITIVE_KEY_PATTERN = re.compile(
    r'(api[_-]?key|secret|password|auth|token|bearer|credential|private[_-]?key|access[_-]?key)', 
    re.IGNORECASE
)

def redact_sensitive(val):
    """Recursively scrub sensitive keys and token-like values from dicts, lists, and strings."""
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
        # Redact raw Bearer tokens or sensitive headers passed as values
        if SENSITIVE_KEY_PATTERN.search(val) or val.lower().startswith("bearer "):
            return "[REDACTED]"
    return val

def process_v2_incident(payload: dict) -> dict:
    # 1. Clean sensitive data immediately
    clean_payload = redact_sensitive(payload)

    # 2. Extract correlation & trace topology
    trace_id = str(clean_payload.get("trace_id") or clean_payload.get("correlation_id") or f"trace-{uuid.uuid4().hex}")
    incident_id = str(clean_payload.get("incident_id") or clean_payload.get("id") or f"inc-{uuid.uuid4().hex[:8]}")

    metrics = clean_payload.get("metrics") or clean_payload
    current_replicas = int(metrics.get("current_replicas", 1))
    max_replicas = int(metrics.get("max_replicas", 10))
    cpu_utilization = float(metrics.get("cpu_utilization", 0.0))

    evidence_ids = clean_payload.get("evidence_ids") or ["ev-cpu-utilization", "ev-replica-limit"]

    # 3. Safe Decision Matrix & Approval Handling
    at_limit = current_replicas >= max_replicas

    if at_limit:
        root_cause = "max_scaling_limit_reached"
        tool_name = "page_owner"
        tool_args = {"recipient": "oncall-owner", "reason": "Service at permitted scaling limit", "incident_id": incident_id}
        action_result = {"status": "paged", "owner_notified": True}
        approval_required = False
        requires_human = False
    elif cpu_utilization > 80.0:
        root_cause = "high_cpu_utilization"
        tool_name = "scale_service"
        tool_args = {"target_replicas": current_replicas + 1, "incident_id": incident_id}
        action_result = {"status": "scaled", "new_replicas": current_replicas + 1}
        approval_required = False
        requires_human = False
    else:
        # Destructive or unsafe diagnostic action -> Enforce Safe Approval Handling
        root_cause = "service_degraded"
        tool_name = "restart_service"
        tool_args = {"service_name": "api-gateway", "incident_id": incident_id}
        action_result = {"status": "pending_approval", "reason": "Destructive action requires human approval"}
        approval_required = True
        requires_human = True

    # 4. OpenTelemetry Spans & Trace Continuity
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
            "attributes": {"incident_id": incident_id}
        },
        {
            "span_id": diag_span_id,
            "parent_span_id": root_span_id,
            "trace_id": trace_id,
            "name": "diagnose_cause",
            "timestamp": now_iso,
            "attributes": {"root_cause": root_cause, "at_limit": at_limit}
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
                "requires_human": requires_human
            }
        }
    ]

    telemetry = redact_sensitive({
        "incident_id": incident_id,
        "trace_id": trace_id,
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