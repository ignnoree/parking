FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    supervisor \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV LIBGL_ALWAYS_INDIRECT=1
ENV DISPLAY=

RUN python -m pip install --upgrade --no-cache-dir pip setuptools wheel

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/logs /app/collection \
    /app/uploads/temp \
    /app/uploads/known_parking_logs/sources /app/uploads/known_parking_logs/crops \
    /app/uploads/unknown_parking_logs/sources /app/uploads/unknown_parking_logs/crops

ENV PARKING_APP_DIR=/app
ENV PARKING_APP_PYTHON=python

EXPOSE 5000

CMD ["supervisord", "-n", "-c", "/app/supervisor/supervisord.conf"]
