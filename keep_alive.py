import os
import threading
from flask import Flask, request, abort, Response

app = Flask(__name__)

@app.get("/")
def home():
    return Response("Bot is running", mimetype="text/plain")

@app.get("/healthz")
def healthz():
    token = os.getenv("KEEPALIVE_TOKEN")
    if token and request.args.get("t") != token:
        abort(403)
    return Response("healthy", mimetype="text/plain")

def _run():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

def keep_alive():
    t = threading.Thread(target=_run, daemon=True)
    t.start()