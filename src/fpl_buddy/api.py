"""HTTP surface: health, proposal reads, and the one-tap approval flow.

Design notes worth keeping:

* ``GET /a/{token}`` only *renders*. Link scanners and mail clients prefetch
  URLs, so a GET that approved would hand your captaincy to a spam filter. The
  buttons on that page POST back to the same URL.
* Mutating endpoints need a signed token scoped to the specific proposal. The
  token is the credential; there is no session, no login, nothing to leak.
* Reads can be gated behind ``API_KEY``. Left empty (the local default) they are
  open, which is fine on localhost and not fine on a public URL -- set the key
  when you deploy.

Settings and the orchestrator hang off ``app.state`` and are reached through
module-level dependencies, so the annotations resolve under
``from __future__ import annotations``.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Template

from .approval import InvalidApprovalToken, read_token, review_url
from .config import Settings, get_settings
from .decisions.executor import ExecutionBlocked
from .decisions.schema import Proposal
from .notify import render_proposal
from .orchestrator import NotActionable, Orchestrator, ProposalNotFound

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- dependencies


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_orchestrator(request: Request) -> Orchestrator:
    """Built lazily, so importing this module never touches FPL or Azure."""
    state = request.app.state
    if getattr(state, "orchestrator", None) is None:
        state.orchestrator = Orchestrator(state.settings)
    return state.orchestrator


def require_api_key(
    request: Request, x_api_key: Annotated[str | None, Header()] = None
) -> None:
    expected = request.app.state.settings.api_key.get_secret_value()
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key.")


Conf = Annotated[Settings, Depends(get_app_settings)]
Orch = Annotated[Orchestrator, Depends(get_orchestrator)]


def resolve_token(settings: Settings, token: str, proposal_id: str | None = None) -> str:
    try:
        signed_id = read_token(settings, token)
    except InvalidApprovalToken as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if proposal_id is not None and signed_id != proposal_id:
        raise HTTPException(status_code=403, detail="This token is for a different proposal.")
    return signed_id


# ----------------------------------------------------------------------- app


def create_app(
    settings: Settings | None = None, orchestrator: Orchestrator | None = None
) -> FastAPI:
    app = FastAPI(
        title="fpl-buddy",
        description="Proposes FPL moves, waits for you, commits at the deadline.",
        version="0.1.0",
    )
    app.state.settings = settings or get_settings()
    app.state.orchestrator = orchestrator

    # ------------------------------------------------------------------ health
    @app.get("/healthz")
    def healthz(conf: Conf) -> dict:
        return {
            "status": "ok",
            "dry_run": conf.dry_run,
            "auto_commit": conf.auto_commit_enabled,
            "entry_id": conf.fpl_entry_id,
        }

    # ------------------------------------------------------------------- reads
    @app.get("/proposals/latest", dependencies=[Depends(require_api_key)])
    def latest(orch: Orch, conf: Conf) -> JSONResponse:
        proposal = orch.latest()
        if proposal is None:
            raise HTTPException(status_code=404, detail="No proposals yet.")
        return JSONResponse(_public(proposal, conf))

    @app.get("/proposals/{proposal_id}", dependencies=[Depends(require_api_key)])
    def one(proposal_id: str, orch: Orch, conf: Conf) -> JSONResponse:
        proposal = orch.get(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="No such proposal.")
        return JSONResponse(_public(proposal, conf))

    # --------------------------------------------------------- approval flow
    @app.get("/a/{token}", response_class=HTMLResponse)
    def review(token: str, orch: Orch, conf: Conf, request: Request) -> HTMLResponse:
        """Render the proposal. Deliberately side-effect free."""
        proposal_id = resolve_token(conf, token)
        proposal = orch.get(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="That proposal no longer exists.")
        _subject, text, _html = render_proposal(proposal, conf)
        return HTMLResponse(
            REVIEW_PAGE.render(
                p=proposal,
                body=text,
                action_url=str(request.url),
                actionable=not proposal.is_terminal and not proposal.fatal_issues,
                settings=conf,
            )
        )

    @app.post("/a/{token}", response_class=HTMLResponse)
    def act(
        token: str,
        orch: Orch,
        conf: Conf,
        action: Annotated[str, Form()],
        note: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        proposal_id = resolve_token(conf, token)
        proposal = _act(orch, proposal_id, action, note)
        return HTMLResponse(
            RESULT_PAGE.render(
                p=proposal,
                action=action,
                url=review_url(conf, proposal.id),
                settings=conf,
            )
        )

    # ------------------------------------------------------- JSON equivalents
    @app.post("/proposals/{proposal_id}/approve")
    def approve(proposal_id: str, orch: Orch, conf: Conf, token: str) -> JSONResponse:
        resolve_token(conf, token, proposal_id)
        return JSONResponse(_public(_act(orch, proposal_id, "approve", ""), conf))

    @app.post("/proposals/{proposal_id}/reject")
    def reject(
        proposal_id: str, orch: Orch, conf: Conf, token: str, note: str = ""
    ) -> JSONResponse:
        resolve_token(conf, token, proposal_id)
        return JSONResponse(_public(_act(orch, proposal_id, "reject", note), conf))

    @app.post("/proposals/{proposal_id}/amend")
    def amend(
        proposal_id: str, orch: Orch, conf: Conf, token: str, note: str
    ) -> JSONResponse:
        resolve_token(conf, token, proposal_id)
        if not note.strip():
            raise HTTPException(status_code=422, detail="Amending needs a note.")
        return JSONResponse(_public(_act(orch, proposal_id, "amend", note), conf))

    return app


# --------------------------------------------------------------------------- #


def _act(orch: Orchestrator, proposal_id: str, action: str, note: str) -> Proposal:
    """Run a human action, mapping every failure onto an honest status code."""
    try:
        match action:
            case "approve":
                return orch.approve(proposal_id, note=note or None)
            case "reject":
                return orch.reject(proposal_id, note=note or None)
            case "amend":
                if not note.strip():
                    raise HTTPException(
                        status_code=409, detail="Amending needs a note saying what to change."
                    )
                return orch.amend(proposal_id, note)
            case _:
                raise HTTPException(status_code=409, detail=f"Unknown action '{action}'.")
    except ProposalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NotActionable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExecutionBlocked as exc:
        raise HTTPException(
            status_code=409, detail=f"Re-validation blocked this at submit time: {exc}"
        ) from exc


def _public(proposal: Proposal, settings: Settings) -> dict:
    """Trim the stored record for the wire.

    The context snapshot and agent transcript are big and only interesting when
    debugging, so they are dropped rather than shipped on every read.
    """
    data = proposal.model_dump(mode="json")
    data.pop("context_snapshot", None)
    data.pop("agent_transcript", None)
    data["headline"] = proposal.headline()
    data["is_executable"] = proposal.is_executable
    data["review_url"] = review_url(settings, proposal.id)
    return data


STYLE = """
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
         max-width: 34rem; margin: 0 auto; padding: 1.5rem 1.25rem 4rem;
         line-height: 1.5; }
  h1 { font-size: 1.35rem; margin: 0 0 .2rem; }
  .sub { color: #666; margin: 0 0 1.25rem; }
  pre { white-space: pre-wrap; background: rgba(127,127,127,.12); padding: 1rem;
        border-radius: .5rem; font-size: .85rem; overflow-x: auto; }
  .bad { background: rgba(211,47,47,.12); border-left: 4px solid #d32f2f;
         padding: .75rem 1rem; border-radius: .25rem; }
  .ok { background: rgba(46,125,50,.12); border-left: 4px solid #2e7d32;
        padding: .75rem 1rem; border-radius: .25rem; }
  form { display: flex; flex-direction: column; gap: .75rem; margin-top: 1.5rem; }
  textarea { width: 100%; box-sizing: border-box; padding: .6rem; border-radius: .4rem;
             border: 1px solid rgba(127,127,127,.5); font: inherit; }
  .row { display: flex; gap: .6rem; flex-wrap: wrap; }
  button { flex: 1 1 8rem; padding: .8rem 1rem; border: 0; border-radius: .4rem;
           font: inherit; font-weight: 600; cursor: pointer; }
  .primary { background: #1b5e20; color: #fff; }
  .danger { background: #b71c1c; color: #fff; }
  .neutral { background: rgba(127,127,127,.25); }
"""

REVIEW_PAGE = Template(
    """
<!doctype html>
<html lang="en"><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GW{{ p.gameweek }} proposal</title>
  <style>"""
    + STYLE
    + """</style>
</head><body>
  <h1>Gameweek {{ p.gameweek }}</h1>
  <p class="sub">{{ p.headline() }} &middot; status: {{ p.status.value }}</p>

  {% if p.fatal_issues %}
  <p class="bad"><strong>Failed validation.</strong> This cannot be submitted:
    {% for i in p.fatal_issues %}<br>&bull; {{ i.message }}{% endfor %}</p>
  {% elif p.is_terminal %}
  <p class="ok">Already {{ p.status.value }} &mdash; nothing further will happen.</p>
  {% endif %}

  {% if settings.dry_run %}
  <p class="ok">DRY_RUN is on: approving will not send anything to FPL.</p>
  {% endif %}

  <pre>{{ body }}</pre>

  {% if actionable %}
  <form method="post" action="{{ action_url }}">
    <textarea name="note" rows="3"
      placeholder="Optional note. Required to amend, e.g. 'captain Oakley instead'"></textarea>
    <div class="row">
      <button class="primary" name="action" value="approve" type="submit">Approve</button>
      <button class="neutral" name="action" value="amend" type="submit">Amend</button>
      <button class="danger" name="action" value="reject" type="submit">Reject</button>
    </div>
  </form>
  {% endif %}
</body></html>
"""
)

RESULT_PAGE = Template(
    """
<!doctype html>
<html lang="en"><head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ p.status.value }}</title>
  <style>"""
    + STYLE
    + """</style>
</head><body>
  <h1>{{ p.status.value|replace("_", " ")|title }}</h1>
  <p class="sub">GW{{ p.gameweek }} &middot; {{ p.headline() }}</p>
  {% if p.execution_error %}
  <p class="bad">{{ p.execution_error }}</p>
  {% elif p.status.value in ("executed", "auto_executed") %}
  <p class="ok">Submitted to FPL{% if settings.dry_run %} (DRY_RUN &mdash; not really){% endif %}.</p>
  {% endif %}
  <p><a href="{{ url }}">Back to the proposal</a></p>
</body></html>
"""
)


# Uvicorn target: `uvicorn fpl_buddy.api:app`.
app = create_app()
