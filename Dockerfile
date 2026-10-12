FROM python:3.12-slim

# FFmpeg does the audio conversion; yt-dlp calls it too.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Downloads and converted files live here; mount a volume to keep them across restarts.
ENV DATA_DIR=/data
# Public-deployment defaults: limit each visitor, and trust the host's one proxy for their address.
ENV RATE_LIMIT_PER_MINUTE=30
ENV TRUSTED_PROXIES=1
RUN useradd --create-home offlify && mkdir -p /data && chown offlify /data
USER offlify

# Jobs are tracked in memory, so this must stay at ONE worker process (threads are fine).
# Hosts such as Render and Railway set PORT; 8000 is the fallback.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 8 --timeout 120 'app:create_app()'"]
