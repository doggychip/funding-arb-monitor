FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN groupadd --gid 10001 app \
  && useradd --uid 10001 --gid app --create-home app \
  && mkdir -p /data \
  && chown -R app:app /app /data

ENV FUNDING_ARB_DB=/data/funding_arb.db
ENV FUNDING_ARB_SCHEDULER=1
ENV FUNDING_ARB_TIMEZONE=Asia/Hong_Kong
VOLUME ["/data"]
EXPOSE 8080
USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8080\")}/healthz', timeout=3)"

CMD ["python", "-m", "funding_arb_monitor.container"]
