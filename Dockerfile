FROM python:3.12-slim

# sopc compiles the prompts. sopserve needs `sopc validate --json` and `sopc export`, first released after v0.0.7.
ARG SOPC_REF=v0.0.8
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://raw.githubusercontent.com/amanmibra/sopc/main/install.sh | SOPC_REF=$SOPC_REF SOPC_INSTALL_DIR=/usr/local/bin sh \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres]"

# Set DATABASE_URL to Postgres in production; the SQLite default lives in the /data volume.
RUN mkdir /data && chown nobody /data
VOLUME /data
ENV HOST=0.0.0.0 PORT=8484 SOPC_BIN=/usr/local/bin/sopc DATABASE_URL=sqlite:////data/sopserve.db
EXPOSE 8484
USER nobody
CMD ["sopserve"]
