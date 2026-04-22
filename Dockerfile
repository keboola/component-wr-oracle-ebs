FROM python:3.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /code/

# ── Oracle Instant Client (Basic Light) ─────────────────────────────────
# Required for connecting to Oracle EBS servers with sec_case_sensitive_logon=FALSE
# or users with only the legacy 10G password verifier. Installed under Oracle
# Free Use Terms and Conditions (FUTC) — freely redistributable in applications.
# https://www.oracle.com/downloads/licenses/oracle-free-license.html
ARG INSTANT_CLIENT_URL="https://download.oracle.com/otn_software/linux/instantclient/2380000/instantclient-basiclite-linux.x64-23.8.0.25.04.zip"
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates libaio1 unzip \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/oracle \
    && curl -fsSL -o /tmp/ic.zip "${INSTANT_CLIENT_URL}" \
    && unzip -q /tmp/ic.zip -d /opt/oracle \
    && rm /tmp/ic.zip \
    && ln -sfn /opt/oracle/instantclient_* /opt/oracle/instantclient \
    && echo "/opt/oracle/instantclient" > /etc/ld.so.conf.d/oracle-instantclient.conf \
    && ldconfig
ENV LD_LIBRARY_PATH="/opt/oracle/instantclient:${LD_LIBRARY_PATH}"

# ── Python deps ─────────────────────────────────────────────────────────
COPY pyproject.toml .
COPY uv.lock .

ENV UV_PROJECT_ENVIRONMENT="/usr/local/"
RUN uv sync --no-dev --frozen

COPY src/ src/

# ── test stage ──
FROM base AS test
RUN uv sync --all-groups --frozen
COPY tests/ tests/
RUN uv run ruff check src/ tests/
CMD ["uv", "run", "pytest", "tests/", "-v"]

# ── production stage ──
FROM base AS production
CMD ["python", "-u", "/code/src/component.py"]
