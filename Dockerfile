FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY amazonorders_mcp ./amazonorders_mcp

RUN pip install --no-cache-dir .

ENTRYPOINT ["amazon-orders-mcp"]
