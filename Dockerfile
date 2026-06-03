FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    WEB_REPORT_ROOT=/app/reports

COPY . /app

RUN mkdir -p /app/reports

VOLUME ["/app/reports"]
EXPOSE 8080

CMD ["python", "-m", "web.server"]
