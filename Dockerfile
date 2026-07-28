# A2Z Core — single monolith image (CLAUDE.md §2: one image, one task family).
# Single-box MVP (docs/architecture/single-box-mvp.md): the Omni-Channel
# worker is NOT a second container/entrypoint -- it runs as a FastAPI
# lifespan background task inside this same process, gated on
# RUN_OMNICHANNEL_WORKER (set by user-data.sh on the deployed box, unset
# everywhere else including tests).

# --- builder: install runtime deps into a venv ---
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir .

# --- runtime ---
FROM python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 1000 app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/app /srv/app/app
WORKDIR /srv/app
USER app
EXPOSE 8000
# slim has no curl; probe /health with stdlib so we add zero packages.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"
# --workers 1 is load-bearing, not a default: core.realtime's pub/sub broker
# and core.rate_limit's sliding windows are plain module-global state with no
# cross-process transport (see app/services/omnichannel/worker.py's
# run_forever docstring). A second uvicorn worker would get its own copies of
# both -- silently doubling every rate limit and dropping realtime updates
# published from whichever process a given SSE stream isn't connected to.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
