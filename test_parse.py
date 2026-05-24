import requests

r = requests.post("http://127.0.0.1:8000/api/parse", json={
    "license_key":      "TEST-1234-5678-ABCD",
    "machine_id":       "test-machine",
    "internal_date_ms": 1716000000000,
    "allowed_vehicles": ["LARGE STRAIGHT"],
    "max_radius_miles": 300,
    "bid_template":     "Rate: $\nDims: {truck_dimensions}\nMC# 1616501\n\nTruck is {google_deadhead} miles out\n{truck_equipment}\n\nETA to PU: {deadhead_eta_str}\n\nALL BIDS ARE VALID 15 MIN",
    "trucks": [{
        "vehicle":         "LARGE STRAIGHT",
        "driver_name":     "John Smith",
        "dimensions":      "264x97x103",
        "max_payload_lbs": 26000,
        "zip_location":    "44129",
        "equipment":       "Lift Gate",
        "allowed_states":  None,
        "pickup_date":     "",
    }],
    "email_body": """Bid on Order #99999
Vehicle required: LARGE STRAIGHT
Pick-Up:
Chicago, IL 60601
01/15/2025 08:00 AM EST
Delivery:
Detroit, MI 48201
01/15/2025 14:00 PM EST
Weight: 10000 lbs
Pieces: 5
Broker Name: John Broker
Broker Phone: 555-123-4567
""",
})

print("Status:", r.status_code)
import json
print(json.dumps(r.json(), indent=2))