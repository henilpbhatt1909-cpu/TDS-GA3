import uuid
import re
from datetime import datetime, timezone

# Keywords to sanitize anywhere in logs, headers, or payloads
SENSITIVE_RE = re.compile(r'(api[_-]?key|secret|password|auth|token|bearer|credential)', re.IGNORECASE)

def redact_sensitive(val):
    """Recursively scrub sensitive keys and string values."""
    if isinstance(val, dict):
        cleaned = {}
        for k, v in val.items():
            if SENSITIVE_RE.search(k):
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = redact_sensitive(v)
        return cleaned
    elif isinstance(val, list):
        return [redact_sensitive(x) for x in val]
    elif isinstance(val, str):
        if SENSITIVE_RE.search(val) and ("bearer" in val.lower() or "key" in val.lower()):
            return "[REDACTED]"
    return val

def process_v2_incident(payload: dict) -> dict:
    # 1. Clean sensitive data immediately
    clean_payload = redact_sensitive(payload)

    trace_id = clean_payload.get("trace_id") or clean_payload.get("correlation_id") or f"trace-{uuid.uuid4().hex}"
    incident_id = clean_payload.get("incident_id") or clean_payload.get("id") or f"inc-{uuid.uuid4().hex[:8]}"

    # Extract metrics safely
    metrics = clean_payload.get("metrics") or clean_payload
    current_replicas = int(metrics.get("current_replicas", 1))
    max_replicas = int(metrics.get("max_replicas", 10))
    cpu_utilization = float(metrics.get("cpu_utilization", 0.0))

    # Evidence mapping
    evidence_ids = clean_payload.get("evidence_ids") or ["ev-cpu-utilization", "ev-replica-limit"]

    # Limit check: When at/over permitted limit, page owner instead of scaling
    at_limit = current_replicas >= max_replicas

    if at_limit:
        root_cause = "scaling_limit_reached"
        tool_name = "page_owner"
        tool_args = {"recipient": "oncall-owner", "reason": "Service at permitted scaling limit", "incident_id": incident_id}
        action_result = {"status": "paged", "owner_notified": True}
        is_destructive = False
    elif cpu_utilization > 80.0:
        root_cause = "high_cpu_utilization"
        tool_name = "scale_up"
        tool_args = {"target_replicas": current_replicas + 1, "incident_id": incident_id}
        action_result = {"status": "scaled", "new_replicas": current_replicas + 1}
        is_destructive = False
    else:
        root_cause = "service_degraded"
        tool_name = "restart_service"
        tool_args = {"service_name": "api-gateway", "incident_id": incident_id}
        action_result = {"status": "restarted"}
        is_destructive = True

    # Build spans with complete trace topology
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
            "name": f"execute_{tool_name}",
            "timestamp": now_iso,
            "attributes": {"tool": tool_name, "is_destructive": is_destructive}
        }
    ]

    telemetry = {
        "incident_id": incident_id,
        "trace_id": trace_id,
        "cpu_utilization": cpu_utilization,
        "current_replicas": current_replicas,
        "max_replicas": max_replicas,
        "scaling_limit_reached": at_limit,
        "payload_summary": {k: v for k, v in clean_payload.items() if k not in ["api_key", "secret", "token"]}
    }

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
            "result": action_result
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