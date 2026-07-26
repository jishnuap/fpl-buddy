# Two stages: build the wheel with build deps present, then ship a slim runtime
# with no compiler and no source tree.
FROM python:3.12-slim AS build

WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip build && python -m build --wheel


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    STATE_DIR=/data \
    PORT=8080

# Non-root: nothing here needs privileges, and the cookie cache is the one
# secret on disk.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data && chown app:app /data

COPY --from=build /build/dist/*.whl /tmp/dist/
# Resolve the wheel filename first: `/tmp/dist/*.whl[azure]` looks like a glob
# with a character class, so the shell matches nothing and pip gets the literal
# pattern. Extras have to be appended to an already-expanded path.
RUN wheel="$(ls /tmp/dist/*.whl)" \
    && pip install "${wheel}[azure]" \
    && rm -rf /tmp/dist

USER app
WORKDIR /home/app
VOLUME ["/data"]
EXPOSE 8080

# By default the scheduler lives in this process, so the container must stay up
# between gameweeks. The other way to run the same image is as a cron job --
# `fpl-buddy tick`, alongside a web replica with SCHEDULER_ENABLED=false, which
# then scales to zero. See docs/serverless.md.
#
# The healthcheck only applies to the long-running form; a job overrides the
# entrypoint and exits, and Docker does not health-check a container that has
# already finished.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;\
urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8080')}/healthz\").read()"

CMD ["python", "-m", "fpl_buddy.main"]
