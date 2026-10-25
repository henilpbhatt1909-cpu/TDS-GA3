import os
from flask import Flask, request, jsonify
from database import init_db, save_telemetry, get_telemetry_history
from triage import diagnose_and_triage

app = Flask(__name__)

# Initialize DB when server starts
init_db()

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "service": "incident-agent"}), 200

@app.route("/triage", methods=["POST"])
def triage_endpoint():
    data = request.get_json() or {}
    
    # 1. Run triage rules
    result = diagnose_and_triage(data)
    
    # 2. Log telemetry to audit store
    save_telemetry(
        incident_id=result["telemetry"]["incident_id"],
        action_taken=result["action"],
        scaling_limit_reached=result["at_limit"],
        telemetry=result["telemetry"]
    )
    
    # 3. Return JSON output
    return jsonify({
        "status": "success",
        "decision": result["action"],
        "scaling_limit_reached": result["at_limit"],
        "telemetry": result["telemetry"]
    }), 200

@app.route("/telemetry", methods=["GET"])
def get_telemetry():
    records = get_telemetry_history()
    return jsonify({"telemetry_history": records}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)