"""Minimal Flask receiver that verifies webhook signatures before acting.
Always verify against the RAW body, before json.loads.

    pip install flask
    SERVER4AGENT_WEBHOOK_SECRET=whsec_... flask --app webhook_receiver run
"""
import json
import os

from flask import Flask, request

from server4agent import verify_webhook_signature

app = Flask(__name__)


@app.post("/hooks/server4agent")
def hook():
    raw = request.get_data(as_text=True)  # raw body
    ok = verify_webhook_signature(
        os.environ["SERVER4AGENT_WEBHOOK_SECRET"],
        raw,
        request.headers.get("Server4Agent-Signature"),
    )
    if not ok:
        return "invalid signature", 401

    event = json.loads(raw)
    if event["event"] == "deployment.live":
        print("live at", event["url"])
    elif event["event"] == "task.failed":
        print("task failed:", event.get("task_id"))
    else:
        print("event:", event["event"])
    return "", 200
