import os
from flask import Flask, request, jsonify
from database import init_db, save_telemetry, get_telemetry_history
from triage import process_v2_incident, redact_sensitive

app = Flask(__name__)
init_db()

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "service": "incident-agent"}), 200

@app.route("/v2/incidents", methods=["POST"])
def incidents_v2_endpoint():
    data = request.get_json(silent=True)
    
    # Validation probe check: return HTTP 400 for bad/empty JSON payloads
    if data is None or not isinstance(data, dict):
        return jsonify({"error": "Invalid or missing JSON payload"}), 400

    try:
        # Scrub incoming headers or body data
        cleaned_payload = redact_sensitive(data)
        response_data = process_v2_incident(cleaned_payload)
        
        # Save telemetry for state storage & durability replay tests
        save_telemetry(
            incident_id=response_data["incident_id"],
            action_taken=response_data["action"]["tool"],
            scaling_limit_reached=response_data["telemetry"]["scaling_limit_reached"],
            telemetry=response_data["telemetry"]
        )
        
        return jsonify(response_data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 422

@app.route("/telemetry", methods=["GET"])
def get_telemetry():
    records = get_telemetry_history()
    return jsonify({"telemetry_history": redact_sensitive(records)}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)