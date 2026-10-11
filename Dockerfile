FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIDEOTONOTES_MODE=server \
    VIDEOTONOTES_WORKSPACE=/data \
    WHISPER_CACHE_DIR=/data/_model_cache \
    MAX_CONCURRENT_TASKS=5

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 videotonotes \
    && useradd --uid 10001 --gid videotonotes --create-home videotonotes

WORKDIR /app
COPY backend/requirements.txt /tmp/requirements.txt
RUN python -c "from pathlib import Path; p=Path('/tmp/requirements.txt'); p.write_text('\n'.join(x for x in p.read_text().splitlines() if not x.startswith('pystray'))+'\n')" \
    && pip install --no-cache-dir -r /tmp/requirements.txt
COPY backend ./backend
COPY frontend ./frontend
COPY sources/icon.png sources/icon.ico ./sources/
RUN mkdir /data && chown -R videotonotes:videotonotes /app /data
USER videotonotes
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request; origin=os.environ.get('VIDEOTONOTES_PUBLIC_ORIGIN','http://127.0.0.1:8000'); from urllib.parse import urlsplit; request=urllib.request.Request('http://127.0.0.1:8000/api/health',headers={'Authorization':'Bearer '+os.environ['VIDEOTONOTES_ACCESS_TOKEN'],'Host':urlsplit(origin).netloc}); urllib.request.urlopen(request,timeout=4).close()"
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
