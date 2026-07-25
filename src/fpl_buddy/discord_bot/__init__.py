"""Discord front end: a gateway bot that posts proposals and takes button taps.

Buttons are another door onto the exact same ``Orchestrator`` methods the web
approval page and the CLI already call -- no decision logic lives here. See
``docs/decisions.md`` for why the buttons are dynamic (survive a restart)
and why every write goes through ``asyncio.to_thread``.
"""
