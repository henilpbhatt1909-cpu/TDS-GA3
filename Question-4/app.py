import os
from flask import Flask, request, jsonify
from database import init_db, save_telemetry, get_telemetry_history
from triage import process_v2_incident, redact_sensitive

app = Flask(__name__)
init_db()

REQUIRED_KEYS = {"incident_id", "id", "metrics", "current_replicas", "cpu_utilization", "logs", "trace_id"}

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "service": "incident-agent"}), 200

@app.route("/v2/incidents", methods=["POST"])
def incidents_v2_endpoint():
    # Enforce JSON request
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 400

    data = request.get_json(silent=True)

    # 1. Validation Probe: Empty, non-dict, or missing schema fields -> HTTP 400
    if data is None or not isinstance(data, dict) or len(data) == 0:
        return jsonify({"error": "Invalid or missing JSON payload"}), 400

    # Verify that payload contains at least one recognized incident key
    if not any(k in data for k in REQUIRED_KEYS):
        return jsonify({"error": "Missing required incident fields in payload"}), 400

    try:
        cleaned_payload = redact_sensitive(data)
        response_data = process_v2_incident(cleaned_payload)

        # Durability check
        save_telemetry(
            incident_id=response_data["incident_id"],
            action_taken=response_data["action"]["tool"],
            scaling_limit_reached=response_data["telemetry"]["scaling_limit_reached"],
            telemetry=response_data["telemetry"]
        )

        return jsonify(response_data), 200
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 422
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/telemetry", methods=["GET"])
def get_telemetry():
    records = get_telemetry_history()
    return jsonify({"telemetry_history": redact_sensitive(records)}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)