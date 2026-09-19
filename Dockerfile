FROM python:3.13-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PIP_NO_CACHE_DIR=1
RUN pip install uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" DATA_DIR=/data TZ=Europe/Amsterdam
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY clothing_advisor ./clothing_advisor
# The compose file runs this as the host user (PUID/PGID) so the ./data bind mount stays writable.
RUN mkdir /data && chmod 777 /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
# No access log: request URLs may carry the access token.
CMD ["uvicorn", "clothing_advisor.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
