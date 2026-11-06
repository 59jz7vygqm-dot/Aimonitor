FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Abhängigkeiten zuerst: ändert sich der Code, bleibt diese Schicht im Cache.
COPY server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Welcher Commit steckt in diesem Image. Ohne das ist von außen nicht zu
# erkennen, ob ein laufender Container den erwarteten Stand hat - und man
# sucht Fehler in Code, der gar nicht läuft.
ARG AIMON_BUILD=unbekannt
ENV AIMON_BUILD=$AIMON_BUILD

COPY server/aimon /app/aimon
COPY server/migrations /app/migrations
COPY web /web
COPY docs /docs

# Nicht als root laufen.
RUN useradd --system --uid 10001 --home /app aimon && chown -R aimon /app
USER aimon

EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/api/v1/health', \
timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "aimon.main:app", \
     "--host", "0.0.0.0", "--port", "8420", "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
