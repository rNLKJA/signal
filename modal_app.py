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


@app.function(image=image)
@modal.asgi_app()
def api():
    from signalkit.api import app as fastapi_app

    return fastapi_app
