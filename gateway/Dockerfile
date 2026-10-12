FROM python:3.13-slim
RUN pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn>=0.32,<1" "httpx>=0.27,<1"
COPY gateway/app.py /app/app.py
RUN useradd -u 10002 -m gateway && mkdir -p /data && chown gateway /data
USER gateway
EXPOSE 4000
CMD ["uvicorn", "app:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "4000"]
