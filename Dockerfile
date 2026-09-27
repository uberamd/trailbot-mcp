FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
RUN useradd --system --uid 10001 app
# DRONE_COMMIT_SHA is supplied by CI so /healthz can report which build is running.
ARG DRONE_COMMIT_SHA=dev
ENV GIT_COMMIT=$DRONE_COMMIT_SHA
USER app
ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["/app/.venv/bin/trailbot-mcp"]
