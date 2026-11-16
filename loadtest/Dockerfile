# Lightweight image for the load-test stub.  The stub only needs the agent
# protocol types and ScriptedAgents, not FAISS, sentence-transformers or the
# embedding model.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend-requirements.txt
COPY loadtest/requirements.txt ./loadtest-requirements.txt
RUN pip install --no-cache-dir -r backend-requirements.txt -r loadtest-requirements.txt

COPY backend ./backend
COPY loadtest ./loadtest

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 9000
CMD ["uvicorn", "loadtest.stub_llm:app", "--host", "0.0.0.0", "--port", "9000"]
