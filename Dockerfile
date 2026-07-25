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

COPY --from=build /build/dist/*.whl /tmp/
RUN pip install /tmp/*.whl[azure] && rm -f /tmp/*.whl

USER app
WORKDIR /home/app
VOLUME ["/data"]
EXPOSE 8080

# The scheduler lives in this process, so the container must stay up between
# gameweeks -- do not run it as a job.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;\
urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8080')}/healthz\").read()"

CMD ["python", "-m", "fpl_buddy.main"]
