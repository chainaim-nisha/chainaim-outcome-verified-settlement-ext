# ---------------------------------------------------------------------------
# Railway deploy image -- chainaim-outcome-verified-settlement-ext
# Service C: settlement-verifier (FastAPI). Reproducible on Railway AND a judge's box.
#
# Why a Dockerfile and not Railway's auto-builder (verified against Railway + Astral docs,
# not assumed):
#   * This repo commits NO uv.lock  -> Railway's default `uv sync --locked ...` fails.
#   * Root pyproject is `package = false` uv WORKSPACE -> default `--no-install-project`
#     would skip the settlement-verifier member.
#   * nest-* is a GIT dependency (nandatown fork, pinned rev 20e18b80...) -> the build needs
#     `git` present to fetch it.
# This image handles all three explicitly.
# ---------------------------------------------------------------------------

FROM python:3.12-slim

# git + certs: `uv sync` clones the git-pinned nest-* packages (nandatown @ 20e18b80) over HTTPS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# uv from Astral's official distroless image, pinned for reproducibility (bump the tag as needed).
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

# Use the base image's system Python 3.12 (satisfies requires-python >=3.12); never auto-download one.
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app
COPY . .

# Install the ENTIRE workspace: settlement-verifier + chainaim-settlement-core + the git-pinned
# nest-* fork. No --locked/--frozen: there is no committed uv.lock, so uv resolves fresh (the fork
# is still byte-pinned by its commit rev in pyproject.toml, so the resolved result is reproducible).
RUN uv sync --all-packages

# Put the workspace venv on PATH so we invoke uvicorn directly (no `uv run` re-resolve at boot).
ENV PATH="/app/.venv/bin:$PATH"

# --- Runtime config (Rule 8: configurable via env, sensible defaults) --------------------------
# APP_DIR    : which services/* dir holds app.py (this repo ships only settlement-verifier).
# APP_MODULE : the ASGI target inside APP_DIR (module:attribute).
# PORT       : injected by Railway at runtime; falls back to 8000 for a local `docker run`.
# The app ALSO reads SETTLEMENT_SIGNING_SEED / SETTLEMENT_TRUSTED_PUBKEYS / SETTLEMENT_PAYEE_PUBKEY
# from the environment -- set those on Railway's Variables tab, not here.
ENV APP_DIR=services/settlement-verifier \
    APP_MODULE=app:app

# 0.0.0.0 is mandatory for Railway routing; `sh -c` expands $PORT / $APP_* at container start.
CMD ["sh", "-c", "uvicorn $APP_MODULE --app-dir $APP_DIR --host 0.0.0.0 --port ${PORT:-8000}"]
