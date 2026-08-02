FROM python:3.11-slim

WORKDIR /app

COPY src/ ./src/
COPY config.json ./config.json

ENTRYPOINT ["python", "-u", "src/main.py"]
