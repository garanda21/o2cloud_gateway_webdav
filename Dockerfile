FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fluxbox \
        novnc \
        websockify \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/

RUN python - <<'PY' > /tmp/requirements.txt
import tomllib
from pathlib import Path

config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
deps = list(config["project"]["dependencies"])
deps.extend(config["project"]["optional-dependencies"]["login"])
Path("/tmp/requirements.txt").write_text("\n".join(deps) + "\n", encoding="utf-8")
PY

RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN python -m playwright install --with-deps chromium

COPY src /app/src
COPY docker/entrypoint.sh /app/docker/entrypoint.sh

RUN pip install --no-cache-dir --no-deps .

RUN useradd --create-home --uid 10001 o2gateway \
    && mkdir -p /config /cache /data \
    && chmod +x /app/docker/entrypoint.sh \
    && chown -R o2gateway:o2gateway /config /cache /data /app /ms-playwright

USER o2gateway

EXPOSE 8080 6080
VOLUME ["/config", "/cache", "/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${APP_PORT:-8080}/health || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python", "-m", "o2gateway.main"]
