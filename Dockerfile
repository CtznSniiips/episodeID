# EpisodeID — CPU image (Whisper runs on CPU with int8).
# For NVIDIA GPUs build Dockerfile.cuda instead.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MEDIA_ROOT=/media CONFIG_DIR=/config CACHE_DIR=/cache \
    HF_HOME=/cache/huggingface PUID=99 PGID=100 UMASK=002 PORT=8686

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/config", "/cache"]
EXPOSE 8686
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8686\")}/api/status',timeout=4)"
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
