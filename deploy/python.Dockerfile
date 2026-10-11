FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

ARG GATEWAY_SDK_REF=b69f4b04b1874507985d831ce83467f7d0cc0025

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --no-checkout https://github.com/superno188462/gateway_api.git /gateway_api \
    && git -C /gateway_api checkout --detach "${GATEWAY_SDK_REF}" \
    && test "$(git -C /gateway_api rev-parse HEAD)" = "${GATEWAY_SDK_REF}"

COPY python_server/ /app/

RUN uv sync --frozen --no-dev --no-install-project \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app

USER app
EXPOSE 8002

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app:app --host 0.0.0.0 --port 8002 --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
