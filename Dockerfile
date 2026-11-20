FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project
COPY src ./src
RUN uv sync --locked

ENV PATH="/app/.venv/bin:$PATH" TODO_DB_PATH=/data/todo.db PORT=8080
EXPOSE 8080
CMD ["todo-mcp"]
