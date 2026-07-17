FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="BabelDOC WebUI" \
      org.opencontainers.image.description="Local-first web interface for BabelDOC PDF translation" \
      org.opencontainers.image.source="https://github.com/ccawmiku/BabelDOC-WebUI" \
      org.opencontainers.image.licenses="AGPL-3.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        fontconfig \
        libfreetype6 \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        libhyperscan5 \
        libspatialindex6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY babeldoc ./babeldoc

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir ".[web]" \
    && useradd --create-home --uid 10001 babeldoc \
    && mkdir -p /data /home/babeldoc/.cache/babeldoc \
    && chown -R babeldoc:babeldoc /data /home/babeldoc

USER babeldoc
VOLUME ["/data", "/home/babeldoc/.cache/babeldoc"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=3)"]

ENTRYPOINT ["python", "-m", "babeldoc.webui.app"]
CMD ["--no-browser", "--host", "0.0.0.0", "--port", "8787", "--data-dir", "/data"]
