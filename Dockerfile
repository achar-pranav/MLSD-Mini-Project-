# syntax=docker/dockerfile:1
#
# One image for the API and the worker: both need backend/agents plus the
# retrieval index (faiss, sentence-transformers, numpy) and the corpus index.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf \
    INDEX_DIR=/app/data/index \
    PYTHONPATH=/app

WORKDIR /app

# faiss-cpu and sentence-transformers publish manylinux wheels for 3.12.
# curl is used by the compose healthchecks; build-essential is a fallback for
# platforms without a prebuilt wheel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential \
    && rm -rf /var/lib/apt/lists/*

# Different basenames: two files called requirements.txt would overwrite each other.
COPY requirements.txt ./requirements-root.txt
COPY backend/requirements.txt ./requirements-agents.txt
COPY requirements-service.txt ./requirements-service.txt
# CPU-only torch first: sentence-transformers would otherwise pull the CUDA
# build plus several GB of NVIDIA wheels into the image.
RUN pip install --no-cache-dir --retries 10 --timeout 300 \
        --index-url https://download.pytorch.org/whl/cpu torch
RUN pip install --no-cache-dir --retries 10 --timeout 300 \
        -r requirements-root.txt \
        -r requirements-agents.txt \
        -r requirements-service.txt

# Optional: bake the embedding model into the image.  Pass the model id from
# data/index/manifest.json as a build arg.  The id is never committed here.
ARG EMBEDDING_MODEL=""
RUN if [ -n "$EMBEDDING_MODEL" ]; then \
        case "$EMBEDDING_MODEL" in \
            *"<"*|*">"*) echo "EMBEDDING_MODEL is still the .env.example placeholder; copy embedder.model from data/index/manifest.json" >&2; exit 1 ;; \
        esac; \
        python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['EMBEDDING_MODEL'])"; \
    fi

COPY backend ./backend
COPY data/index ./data/index

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /models/hf /data/runs \
    && chown -R appuser:appuser /app /models/hf /data/runs
USER appuser

EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
