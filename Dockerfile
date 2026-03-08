FROM python:3.11-slim

# FFmpeg + Node.js telepítése
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    npm \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python csomagok
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp frissítés (legújabb verzió)
RUN pip install --no-cache-dir --upgrade yt-dlp

# Alkalmazás másolása
COPY . .

# Render.com a PORT env variable-t használja
EXPOSE 10000

CMD ["python", "radio_server.py"]
