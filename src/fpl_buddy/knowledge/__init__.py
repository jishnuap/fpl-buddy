"""Harvested article knowledge: tips, team news and analysis from the web.

A daily job walks a set of configured sources, finds articles it has not seen
before, extracts and summarises them, and writes one markdown file per article
into ``STATE_DIR/knowledge``. The agent does not read those files as part of its
brief -- it gets a compact index and pulls detail through a tool when it wants
it, so the always-on token cost of a growing archive stays near zero.

Everything here treats fetched text as **data, never instructions**. See
``summarize.py`` for why that matters and what enforces it.
"""
