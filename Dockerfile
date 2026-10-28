# Pinned by digest for reproducible builds; Dependabot proposes updates
# (Debian and Python security fixes) as pull requests that CI tests.
FROM python:3.12-slim-trixie@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
# One codebase, two editions:
#   full (default): everything, including searchable PDFs and auto-straighten
#   lite:           for Raspberry Pi-class hosts; leaves out the OCR stack
#                   (OCRmyPDF, Tesseract) and OpenCV/NumPy
# Both include the built-in print server: CUPS with every printer driver
# Debian packages, because old printers on small hosts need them most.
ARG FOLIO_EDITION=full
ARG FOLIO_VERSION=0.21.0
LABEL org.opencontainers.image.title="Folio Scan Studio" \
      org.opencontainers.image.version="${FOLIO_VERSION}-${FOLIO_EDITION}" \
      org.opencontainers.image.description="Self-hosted scanning, printing and document workbench for CUPS/SANE devices" \
      org.opencontainers.image.source="https://github.com/kmmukut/folio-scan-studio" \
      org.opencontainers.image.url="https://github.com/kmmukut/folio-scan-studio" \
      org.opencontainers.image.documentation="https://github.com/kmmukut/folio-scan-studio#readme" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
RUN case "$FOLIO_EDITION" in full|lite) ;; *) echo "FOLIO_EDITION must be full or lite" >&2; exit 1;; esac \
    && apt-get update && apt-get install -y --no-install-recommends \
       sane-utils sane-airscan cups-client ca-certificates tini \
       cups cups-filters printer-driver-all foomatic-db-compressed-ppds openprinting-ppds hplip \
       $( [ "$FOLIO_EDITION" = full ] && echo ocrmypdf tesseract-ocr-eng ) \
    && rm -rf /var/lib/apt/lists/* \
    # HP's SANE plug-in (from hplip, kept for its printer drivers) aborts every
    # scanner listing when there is no D-Bus, as in a container; HP network
    # scanners are reached through eSCL (sane-airscan) instead.
    && sed -i 's/^hpaio/#hpaio/' /etc/sane.d/dll.d/hplip
WORKDIR /app
COPY requirements.txt requirements-full.txt ./
# pip is needed only here, so it doesn't stay in the image: the base image's
# copy has known flaws, and so do the helpers bundled in newer ones. A current
# pip installs the libraries, then goes. (The image's test run brings one back
# with "python -m ensurepip", in its own throwaway container.)
RUN pip install --no-cache-dir pip==26.2.2 \
    && pip install --no-cache-dir -r "$( [ "$FOLIO_EDITION" = full ] && echo requirements-full.txt || echo requirements.txt )" \
    && pip uninstall --yes --quiet pip
COPY backend ./backend
COPY frontend ./frontend
COPY LICENSE docker-entrypoint.sh ./
RUN mkdir -p /data/jobs /etc/sane.d/airscan.d \
    && ln -s /data/managed-airscan.conf /etc/sane.d/airscan.d/folio-managed.conf
ENV DATA_DIR=/data SCANNER_DEVICE="" FOLIO_EDITION=${FOLIO_EDITION}
# 8870: Folio. 631: the built-in print server, for phones and computers that
# print to it directly (AirPrint, IPP); publish it only if you want that.
EXPOSE 8870 631
# Liveness only: confirms the app answers and its data volume is usable.
HEALTHCHECK --interval=30s --timeout=6s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8870/api/health', timeout=5)"
# tini runs as PID 1: it passes stop signals to Folio and the print server
# (-g: the whole process group) and reaps processes orphaned by stopped
# OCR/scan helpers, which would otherwise linger as zombies.
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/docker-entrypoint.sh"]
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8870", "--workers", "1", "--proxy-headers"]
