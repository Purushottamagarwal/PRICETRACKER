FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TRACKER_DATA_DIR=/data CHECK_INTERVAL_SECONDS=900
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tracker.py .
RUN mkdir -p /data
CMD ["python", "-u", "tracker.py"]
