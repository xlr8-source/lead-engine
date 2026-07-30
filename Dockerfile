FROM python:3.14-slim

WORKDIR /app

# Dependencies pinned and baked in at build time, so a later edit to
# requirements.txt on disk cannot alter an already-built image.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Everything the app reads or executes is copied in at BUILD time --
# db/schema.sql, engine/prompts/*.md, api/, engine/, ingestion/, frontend/.
# None of it is bind-mounted at run time, so the image's executable content
# is fixed at build and cannot be altered by later changes on the host.
COPY api/ ./api/
COPY db/ ./db/
COPY engine/ ./engine/
COPY ingestion/ ./ingestion/
COPY frontend/ ./frontend/

# Deliberately NOT copied: .env, .claude/, .agents/, data/ -- these are
# supplied at run time only, from explicitly chosen paths, never baked into
# the image.

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
