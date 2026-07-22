FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV FUNDING_ARB_DB=/data/funding_arb.db
VOLUME ["/data"]
EXPOSE 8080

CMD ["funding-arb-monitor", "serve", "--host", "0.0.0.0", "--port", "8080"]
