# YuriOS in a container — an optional install method beside ./install.sh.
#
#   docker compose up -d                   # builds this, then starts her (compose.yaml)
#   docker compose logs yurios             # the first boot prints the pairing link
#
# The code lives in /opt/yurios; the installation — .env, data/ (every character's
# Vault, traces and photos), .yurios/ and the model caches — is the /yurios volume.
# Moving her is copying that volume, the same promise a folder makes natively.
# docs/installation.md has the whole story: the owner token, reaching a model on
# the host, and what does not come along (the native window, the tray, a GPU).
#
# Build args:
#   EXTRAS=voice   the default, as install.sh: ears, voice and turn-taking on CPU.
#                  Pass EXTRAS= for a text-only image (~2 GB smaller), or add more
#                  extras comma-separated (EXTRAS=voice,llm).

# --- the frontends: built here so the image needs no Node ---------------------
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- the house -----------------------------------------------------------------
# 3.12 is the installer target; requires-python is >=3.11,<3.14.
FROM python:3.12-slim-bookworm
ARG EXTRAS=voice
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

# git is her Vault's history; espeak-ng (kokoro's phonemiser) and libsndfile are
# the voice's, the same two install.sh adds.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git espeak-ng libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies before the source, so an edit to her code reuses this layer. CPU
# torch first (747 MB, not PyPI's 4.5 GB CUDA build), then the project's
# dependency set held to constraints.txt — the order install.sh uses. A stub
# package stands in for the source so pip can resolve without it.
WORKDIR /opt/yurios
COPY pyproject.toml constraints.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu \
    && mkdir yurios && touch yurios/__init__.py \
    && /opt/venv/bin/pip install -c constraints.txt -e ".${EXTRAS:+[$EXTRAS]}" \
    && rm -rf yurios
ENV PATH=/opt/venv/bin:$PATH

# Editable, as install.sh installs it: the package finds soul-src/ and web/dist/
# beside itself.
COPY . .
COPY --from=web /web/dist web/dist
RUN pip install --no-deps -e . && chmod +x scripts/docker-entrypoint.sh

# The installation. HOST must be every interface inside a container — which port
# that reaches is `docker run -p`'s decision — and a non-loopback bind needs an
# owner token, which the entrypoint makes on first boot. HF_HOME keeps the
# embedder's and the voice's downloaded weights in the volume across rebuilds.
ENV YURIOS_ROOT=/yurios \
    HOST=0.0.0.0 \
    SOUL_SRC=/opt/yurios/soul-src \
    HF_HOME=/yurios/models/huggingface
RUN useradd --create-home --uid 1000 yurios \
    && mkdir /yurios && chown yurios:yurios /yurios
USER yurios
WORKDIR /yurios
VOLUME /yurios
EXPOSE 8768

# Healthy means the server answers. Every route is behind the owner token from
# in here too (HOST=0.0.0.0 puts the boundary on loopback as well), so the 401 a
# tokenless probe gets back is that answer — a dead server raises URLError.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u, urllib.error as e\ntry: u.urlopen('http://127.0.0.1:8768/api/health', timeout=4)\nexcept e.HTTPError: pass"]
ENTRYPOINT ["/opt/yurios/scripts/docker-entrypoint.sh"]
CMD ["yurios", "start", "--foreground"]
