# Signal API — runs anywhere a container runs.
#   docker build -t signal .
#   docker run -p 8000:8000 signal
# The offline data snapshot is bundled, so the container serves immediately
# even without outbound network; live data refreshes in the background.

FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY signalkit ./signalkit
RUN pip install --no-cache-dir .

# The decision log is written to ./logs at runtime; run as non-root.
RUN useradd --system appuser && mkdir -p /app/logs && chown appuser /app/logs
USER appuser

EXPOSE 8000
CMD ["uvicorn", "signalkit.api:app", "--host", "0.0.0.0", "--port", "8000"]
