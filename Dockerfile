FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
# Сначала только зависимости: слой кэшируется и не пересобирается при правке кода
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY companies ./companies
RUN uv sync --frozen --no-dev && useradd --system app
USER app
ENV PATH="/app/.venv/bin:$PATH" HOST=0.0.0.0 PORT=8086
EXPOSE 8086
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8086/health')"
# Миграции и синхронизация знаний при старте (неизменённые документы не пересчитываются)
CMD ["sh", "-c", "support migrate && support sync && support run"]
