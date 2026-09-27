FROM python:3.12-slim

WORKDIR /app

# Requirements first so the dependency layer caches across source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY copilot/ ./copilot/
COPY prompts/ ./prompts/
COPY runbooks/ ./runbooks/
COPY static/ ./static/

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Shell form on purpose: exec form does not expand ${PORT}, and hosts like
# Render assign the port at runtime rather than letting the image pick one.
# Single worker: embedded Qdrant takes an exclusive directory lock. Point
# QDRANT_URL at a Qdrant server or Cloud to scale past one worker.
CMD uvicorn copilot.api:app --host 0.0.0.0 --port ${PORT:-8000}
