"""
Modal deployment for the Signal API.

Deploy (requires a Modal account and `pip install modal && modal setup`):

    modal deploy modal_app.py

The image bundles the whole signalkit package including the offline data
snapshot, so the API serves even if the live NYC Open Data endpoint is slow
or unreachable from the container.
"""

import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("pydantic>=2,<3", "fastapi>=0.110", "uvicorn>=0.29", "httpx>=0.27")
    .add_local_dir("signalkit", remote_path="/root/signalkit")
)

app = modal.App("signal-api")

# The audit trail must survive cold starts — an ephemeral governance log is
# no governance log. Decisions persist to a Modal Volume mounted at /data.
decisions_volume = modal.Volume.from_name("signal-decisions", create_if_missing=True)


# LLM narrative config (SIGNAL_LLM_* vars) lives in the "signal-llm" secret;
# create it with: modal secret create signal-llm SIGNAL_LLM_API_KEY=... etc.
llm_secret = modal.Secret.from_name("signal-llm")


# max_containers=1: the rate limiter and the decision-log file are
# in-process/single-writer by design — one container keeps both globally
# correct, and caps cost. Plenty for a demo's traffic.
# min_containers=1: one container stays resident so a visitor's first
# click never pays a cold start. Costs idle compute (fits Modal's free
# credits at current usage); remove the parameter to go scale-to-zero.
@app.function(
    image=image,
    volumes={"/data": decisions_volume},
    secrets=[llm_secret],
    max_containers=1,
    min_containers=1,
)
@modal.asgi_app()
def api():
    import os

    os.environ.setdefault("SIGNAL_LOG_PATH", "/data/decisions.jsonl")
    from signalkit.api import app as fastapi_app

    return fastapi_app
