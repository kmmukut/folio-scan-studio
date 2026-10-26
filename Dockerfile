FROM python:3.12-slim-bookworm
# One codebase, two editions:
#   full (default): everything, including searchable PDFs and auto-straighten
#   lite:           for Raspberry Pi-class hosts; leaves out the OCR stack
#                   (OCRmyPDF, Tesseract, Ghostscript) and OpenCV/NumPy
ARG FOLIO_EDITION=full
ARG FOLIO_VERSION=0.12.0
LABEL org.opencontainers.image.title="Folio Scan Studio" \
      org.opencontainers.image.version="${FOLIO_VERSION}-${FOLIO_EDITION}" \
      org.opencontainers.image.description="Self-hosted scanning, printing and document workbench for CUPS/SANE devices"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
RUN case "$FOLIO_EDITION" in full|lite) ;; *) echo "FOLIO_EDITION must be full or lite" >&2; exit 1;; esac \
    && apt-get update && apt-get install -y --no-install-recommends \
       sane-utils sane-airscan cups-client ca-certificates tini \
       $( [ "$FOLIO_EDITION" = full ] && echo ocrmypdf tesseract-ocr-eng ) \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-full.txt ./
RUN pip install --no-cache-dir -r "$( [ "$FOLIO_EDITION" = full ] && echo requirements-full.txt || echo requirements.txt )"
COPY backend ./backend
COPY frontend ./frontend
RUN mkdir -p /data/jobs /etc/sane.d/airscan.d \
    && ln -s /data/managed-airscan.conf /etc/sane.d/airscan.d/folio-managed.conf
ENV DATA_DIR=/data SCANNER_DEVICE="" FOLIO_EDITION=${FOLIO_EDITION}
EXPOSE 8870
# Liveness only: confirms the app answers and its data volume is usable.
HEALTHCHECK --interval=30s --timeout=6s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8870/api/health', timeout=5)"
# tini runs as PID 1: it forwards stop signals to Folio and reaps processes
# orphaned by stopped OCR/scan helpers, which would otherwise linger as zombies.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8870", "--workers", "1", "--proxy-headers"]
