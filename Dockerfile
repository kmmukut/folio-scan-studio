FROM python:3.12-slim-bookworm
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
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8870", "--workers", "1"]
