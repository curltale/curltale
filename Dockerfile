# build context: this folder (docker-compose.yml: build: .)
# base images pinned to exact versions (bump deliberately, then run: npm audit / pip-audit, see README)
FROM node:24.22.1-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npx vite build

FROM python:3.13.17-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
# exact versions with hashes; --no-deps because the lock lists every package (and overrides fastembed's pillow pin, see requirements.lock)
COPY backend/requirements.lock ./
RUN pip install --require-hashes --no-deps -r requirements.lock
ENV MODEL_CACHE=/app/models
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-base-en-v1.5', cache_dir='/app/models')"   # the model is baked into the image: no download at runtime
COPY backend/studio ./studio
COPY --from=ui /ui/dist ./frontend/dist
# no ontology is baked in: a new installation starts empty (upload an Excel file or generate a model from a database). SEED_JSON can point at a starting model.
ENV FRONTEND_DIR=/app/frontend/dist
# not root: uid 1000 is the server account, so files the app writes in app/data belong to it
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin studio && mkdir -p /app/data && chown -R studio:studio /app/data /app/models
USER studio
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
# --no-proxy-headers: the app reads forwarded headers itself and only from trusted proxies (clientinfo.py), so a direct caller cannot pose as someone else
CMD ["uvicorn", "studio.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
