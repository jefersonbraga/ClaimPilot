FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AUDIT_DB_PATH=/data/claimpilot.db
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data
COPY evals ./evals
COPY tests ./tests
COPY pyproject.toml .

# Generate the evaluation snapshot shown on the demo page from a real run (mock provider, no key needed).
RUN python -m evals.run > /dev/null

RUN useradd --create-home appuser && mkdir -p /data && chown appuser /data
USER appuser

EXPOSE 8000
HEALTHCHECK CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.getenv(\"PORT\", \"8000\")}/health')"
# PORT is injected by hosting platforms (Railway, Cloud Run, Render...); 8000 locally.
# --proxy-headers: behind a hosting proxy take the client IP from X-Forwarded-For, so the per-client rate limit
# applies per visitor instead of to the proxy. Daily caps still bound any spoofing.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
