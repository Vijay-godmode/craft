FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    RESUME_DB_PATH=/data/careercraft.db

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
RUN mkdir -p /data

EXPOSE 8000
CMD ["sh", "-c", "waitress-serve --host=0.0.0.0 --port=${PORT} app:app"]
