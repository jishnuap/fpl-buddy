# AIrsenal prediction sidecar.
#
# Separate from the fpl-buddy image on purpose. This one carries jax, numpy,
# pandas and a compiler; the fpl-buddy image stays small enough that an idle
# `fpl-buddy tick` measures ~0.9s. See docs/airsenal.md.

FROM python:3.12-slim-bookworm

# git is not optional: AIrsenal depends on `bpl` via a git URL, which is not on
# PyPI. build-essential is for the source builds that pulls in.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential git \
    && rm -rf /var/lib/apt/lists/*

# Pinned to a commit, not a tag: upstream's tags stopped at v1.9.1 while
# pyproject.toml reads 1.15.0, so a tag is not a version here. Bump this
# deliberately, and run one gameweek against the previous artefact before
# trusting the new one.
ARG AIRSENAL_REF=b8d7a157924f42dc1f7ff812fc38beb636405e3f

RUN pip install --no-cache-dir \
    "airsenal @ git+https://github.com/alan-turing-institute/AIrsenal.git@${AIRSENAL_REF}"

COPY dump_predictions.py run.sh /sidecar/
RUN chmod +x /sidecar/run.sh

# Written by run.sh, read by fpl-buddy. Mount the same volume in both.
ENV AIRSENAL_HOME=/data/airsenal/db \
    AIRSENAL_SNAPSHOT_PATH=/data/airsenal/predictions.json \
    AIRSENAL_WEEKS_AHEAD=5

VOLUME ["/data"]

CMD ["/sidecar/run.sh"]
