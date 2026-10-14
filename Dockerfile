# OpenTune Virtual Engine Lab — headless/cloud image (simulation only; no hardware access).
#   docker build -t opentune-lab .
#   docker run --rm -p 127.0.0.1:8765:8765 -v opentune-data:/data opentune-lab
# Then open http://127.0.0.1:8765. Publish the port on 127.0.0.1 only: the lab has no authentication.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    OPENTUNE_DATA_DIR=/data OPENTUNE_LAB_ALLOW_REMOTE=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[lab]" && useradd --create-home --uid 10001 lab && mkdir -p /data && chown lab /data

USER lab
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health').status == 200 else 1)"
# Inside the container the server must listen on all interfaces for port publishing to work.
CMD ["opentune-lab", "serve", "--host", "0.0.0.0", "--port", "8765"]
