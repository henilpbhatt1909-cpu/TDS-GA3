def diagnose_and_triage(incident_data: dict) -> dict:
    """
    Evaluates incident data and returns safe action.
    Rule: If service is at permitted scaling limit, page owner instead of scaling.
    """
    incident_id = incident_data.get("incident_id", "inc-unknown")
    current_replicas = incident_data.get("current_replicas", 1)
    max_replicas = incident_data.get("max_replicas", 10)
    cpu_utilization = incident_data.get("cpu_utilization", 0.0)
    
    # Core requirement safety check
    at_limit = current_replicas >= max_replicas

    if at_limit:
        action = "page_owner"
        reason = f"Service reached scaling limit ({current_replicas}/{max_replicas}). Paging owner."
    elif cpu_utilization > 80.0:
        action = "scale_up"
        reason = f"CPU high ({cpu_utilization}%). Scaled safely."
    else:
        action = "restart_pod"
        reason = "Normal diagnostic load; executed restart."

    telemetry = {
        "incident_id": incident_id,
        "cpu_utilization": cpu_utilization,
        "current_replicas": current_replicas,
        "max_replicas": max_replicas,
        "reason": reason
    }

    return {
        "action": action,
        "at_limit": at_limit,
        "telemetry": telemetry
    }