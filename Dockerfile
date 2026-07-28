FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONPATH=/app/src

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser pyproject.toml uv.lock README.md ./
COPY --chown=appuser:appuser src/ src/

RUN uv sync --no-dev --frozen

USER appuser

CMD ["/app/.venv/bin/python", "-m", "goprofiles_mcp"]
