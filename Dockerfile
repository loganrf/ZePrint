# ZePrint - label printing service for Zebra printers
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/opt/zeprint/mpl \
    ZEPRINT_DATA_DIR=/data \
    ZEPRINT_PORT=8080

WORKDIR /opt/zeprint

# dependencies first, so code changes don't re-download them
COPY pyproject.toml README.md ./
RUN mkdir zeprint && touch zeprint/__init__.py \
 && pip install . \
 && pip uninstall -y zeprint && rm -rf zeprint build *.egg-info

COPY zeprint ./zeprint
RUN pip install --no-deps . \
 && rm -rf build *.egg-info \
 # warm matplotlib's font cache so the first label isn't slow; world-writable
 # because the runtime user (PUID) isn't the build user
 && python -c "import matplotlib.figure, matplotlib.font_manager" \
 && chmod -R a+rwX "$MPLCONFIGDIR" \
 && useradd --system --uid 1000 --user-group --home-dir /data --no-create-home zeprint \
 && mkdir -p /data && chown zeprint:zeprint /data

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('ZEPRINT_PORT','8080')}/api/health\", timeout=4)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["zeprint", "serve"]
