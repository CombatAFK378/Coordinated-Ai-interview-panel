# Build the locked frontend dependencies.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY media-worker/requirements.txt ./media-worker/requirements.txt
RUN pip install --no-cache-dir -r media-worker/requirements.txt
# Both local models are bundled with the pinned SDK/plugins. Fail the build if
# either cannot load on this CPU architecture, instead of degrading silently.
RUN python -c "from livekit.plugins import silero; from livekit.local_inference import EOT; silero.VAD.load(); EOT()"
COPY shared ./shared
COPY media-worker ./media-worker
COPY --from=web /web/dist ./web/dist
ENV PYTHONPATH=/app
ENV WEB_DIR=/app/web/dist
WORKDIR /app/media-worker
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
