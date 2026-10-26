FROM python:3.12-slim-bookworm
ARG FOLIO_VERSION=0.9.6
LABEL org.opencontainers.image.title="Folio Scan Studio" \
      org.opencontainers.image.version="${FOLIO_VERSION}" \
      org.opencontainers.image.description="Self-hosted scanning, printing and document workbench for CUPS/SANE devices"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    sane-utils sane-airscan cups-client ca-certificates ocrmypdf tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY frontend ./frontend
RUN mkdir -p /data/jobs /etc/sane.d/airscan.d \
    && ln -s /data/managed-airscan.conf /etc/sane.d/airscan.d/folio-managed.conf
ENV DATA_DIR=/data SCANNER_DEVICE=""
EXPOSE 8870
# Liveness only: confirms the app answers and its data volume is usable.
HEALTHCHECK --interval=30s --timeout=6s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8870/api/health', timeout=5)"
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8870", "--workers", "1", "--proxy-headers"]
