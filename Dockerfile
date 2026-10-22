FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY webapp.py ./
COPY templates ./templates
COPY static ./static
COPY start.sh ./
RUN chmod +x /app/start.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import socket; s=socket.create_connection(('127.0.0.1', 8080), 3); s.close()"

CMD ["/app/start.sh"]
