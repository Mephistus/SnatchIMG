FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SNATCHIMG_HOST=0.0.0.0 \
    SNATCHIMG_PORT=8080

WORKDIR /app

COPY web_app.py snatchimg.py dev_reload.py ./
COPY static ./static

RUN mkdir -p /app/.snatchimg_runs

RUN pip install --no-cache-dir watchdog

EXPOSE 8080

CMD ["python", "dev_reload.py"]
