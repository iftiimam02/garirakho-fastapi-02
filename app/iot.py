# app/iot.py
import os, json
from azure.iot.hub import IoTHubRegistryManager

IOTHUB_CONNECTION_STRING = os.getenv("IOTHUB_CONNECTION_STRING", "").strip()

def send_c2d(device_id: str, payload: dict):
    if not IOTHUB_CONNECTION_STRING:
        raise RuntimeError("IOTHUB_CONNECTION_STRING not set")
    mgr = IoTHubRegistryManager(IOTHUB_CONNECTION_STRING)
    mgr.send_c2d_message(device_id, json.dumps(payload))
