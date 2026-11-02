FROM node:24-bookworm-slim AS codex-build
RUN npm install -g @openai/codex@0.164.0

FROM python:3.13-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --uid 1000 --create-home importer
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY uni_tasks ./uni_tasks
USER 1000:1000
CMD ["python", "-m", "uni_tasks.auth", "status"]

FROM base AS parser
USER root
COPY --from=codex-build /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-build /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
ENV CODEX_HOME=/codex
USER 1000:1000
CMD ["python", "-m", "uni_tasks.parser"]

FROM base AS worker
CMD ["python", "-m", "uni_tasks.worker"]
