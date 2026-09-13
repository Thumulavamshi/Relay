"""FastAPI app: trigger endpoint, Vapi webhook ingress, read routes.

The rule that shapes this file: the webhook handler acknowledges fast and does
the real work in the background. Vapi is driving a live phone call; anything slow
on this path is heard as a pause by the person on the other end.
"""

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import actions, callbacks, classifier, db, dialler, reconcile, understanding
from . import handlers  # noqa: F401  - importing registers the action handlers
from . import callfacts, gcal, views, whatsapp
from . import integrations  # importing registers the Calendar / HubSpot / Slack handlers
from .config import EVALUATOR_NUMBER, settings

def _build_stamp():
    """Short hash of the app source, so a STALE RUNNING SERVER is visible.

    A live mid-call test failed twice because uvicorn was started without
    --reload and kept serving code from before a fix. Everything looked correct -
    the config, the deploy, the tunnel - while the process itself was old. This
    makes that detectable in one HTTP call instead of a post-mortem.
    """
    import glob
    import hashlib
    h = hashlib.sha256()
    for path in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "*.py"))):
        with open(path, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


BUILD = _build_stamp()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("elevatebox")


@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    log.info("build %s", BUILD)
    log.info("db ready at %s", settings.db_path)
    log.info("allowed destination: %s", settings.allowed_destination or "(unset)")
    if settings.missing():
        log.warning("cannot place calls, missing: %s", ", ".join(settings.missing()))

    # Strong reference held for the app's lifetime. asyncio keeps only a weak one,
    # and a garbage-collected worker would look exactly like "the callback never
    # fired" - the same trap that bites fire-and-forget sends.
    workers = [asyncio.create_task(callbacks.worker()),
               asyncio.create_task(reconcile.worker()),
               # A warm Google token makes the freeBusy inside a live tool call
               # one round trip instead of two. Best-effort; never blocks startup.
               asyncio.create_task(asyncio.to_thread(gcal.warm))]
    try:
        yield
    finally:
        for w in workers:
            w.cancel()
        for w in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await w


app = FastAPI(title="ElevateBox voice agent", version="0.1.0", lifespan=lifespan)


# ----------------------------------------------------------------- health

@app.get("/health")
def health():
    dest = settings.allowed_destination
    return {
        "status": "ok",
        "build": BUILD,
        "features": {
            # If any of these read False on a running server, it is stale.
            "adopts_unknown_calls": True,
            "answers_unattributed_tool_calls": True,
        },
        "db": settings.db_path,
        "env_file": settings.env_file,
        "can_place_calls": not settings.missing(),
        "missing_settings": settings.missing(),
        # Shown so a misconfiguration is obvious before a call, not after.
        "allowed_destination": dest,
        "destination_is_evaluator": _same_number(dest, EVALUATOR_NUMBER),
        # The pre-flight for the one call that matters. "blocked" is obvious;
        # "half-armed" - dialable but not messageable - is not, and it costs
        # every WhatsApp on a call that otherwise looks perfect.
        "evaluator_run": dict(zip(("ready", "detail"), settings.evaluator_ready())),
        "actions": actions.status_report(),
        "understanding": understanding.status_report(),
        "classifier": classifier.providers_status(),
        "whatsapp": dict(zip(("ready", "detail"), whatsapp.configured())),
        "integrations": integrations.status_report(),
    }


@app.get("/api/integrations")
async def integrations_check():
    """Live, read-only check of every connected app: does the token work, and is
    the configuration we act on - calendar, pipeline stages, channel - real?"""
    return {"apps": await integrations.live_check(),
            "status": integrations.status_report()}


EVIDENCE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "data", "evals", "replay-latest.json")


@app.get("/api/evals/latest")
def latest_eval():
    """The latest replay-harness run against the real apps (backend/replay_harness.py)."""
    import json
    if not os.path.exists(EVIDENCE_PATH):
        return {"available": False, "hint": "run: python backend/replay_harness.py"}
    with open(EVIDENCE_PATH, encoding="utf-8") as fh:
        return {"available": True, **json.load(fh)}


# ----------------------------------------------------------------- trigger

def _same_number(a, b):
    digits = lambda s: "".join(ch for ch in (s or "") if ch.isdigit())
    da, dbn = digits(a), digits(b)
    return bool(da) and bool(dbn) and da[-10:] == dbn[-10:]


@app.post("/calls")
async def trigger_call():
    """Place a call to the single configured destination.

    Deliberately takes NO request body and NO phone number. The destination comes
    only from ALLOWED_DESTINATION. That is what makes this endpoint impossible to
    use as an open dialler if the URL leaks - there is no parameter to abuse.
    """
    try:
        return dialler.place()
    except dialler.DialError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


# ----------------------------------------------------------------- page

PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>ElevateBox voice agent</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;min-height:100vh;display:grid;place-items:center;
   background:#0d0d0f;color:#f2f2f3;
   font:16px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
 main{width:min(30rem,90vw);padding:2rem 0}
 h1{font-size:1.35rem;margin:0 0 .4rem}
 p{color:#a1a1aa;margin:.4rem 0 1.6rem}
 button{width:100%;padding:1.1rem;font:600 1.05rem/1 inherit;color:#0d0d0f;
   background:#f2f2f3;border:0;border-radius:.7rem;cursor:pointer}
 button:disabled{opacity:.5;cursor:default}
 #out{margin-top:1.2rem;padding:.9rem 1rem;border-radius:.6rem;
   background:#17171a;border:1px solid #26262b;white-space:pre-wrap;
   font:.85rem/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;display:none}
 small{color:#71717a;display:block;margin-top:1.4rem}
</style>
<main>
 <h1>AI voice sales agent</h1>
 <p>Press the button and the system places the call itself. Nothing to install.</p>
 <button id=go>Call me now</button>
 <div id=out></div>
 <small>Speaks English, Hindi or Telugu &middot; qualifies the lead &middot;
 fires a WhatsApp mid-call on high intent &middot; books callbacks from spoken time.</small>
</main>
<script>
const btn=document.getElementById('go'),out=document.getElementById('out');
const show=t=>{out.style.display='block';out.textContent=t};
btn.onclick=async()=>{
  btn.disabled=true;btn.textContent='Dialling...';show('Placing the call...');
  try{
    const r=await fetch('/calls',{method:'POST'});
    const d=await r.json();
    if(!r.ok)throw new Error(d.detail||JSON.stringify(d));
    show('Ringing '+d.destination+'\\nStatus: '+d.status+'\\nCall id: '+d.call_id);
    btn.textContent='Call placed';
  }catch(e){
    show('Could not place the call.\\n'+e.message);
    btn.disabled=false;btn.textContent='Try again';
  }
};
</script>"""


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/", response_class=HTMLResponse)
def home():
    """The Relay web UI: start a call, watch it live, see every app action and
    the evidence. Static HTML and vanilla JS, no build step - it polls the same
    read routes the API exposes, so it can never show anything the data does not.
    """
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/classic", response_class=HTMLResponse)
def classic():
    """The original one-button page. It posts to /calls, which takes no phone
    number, so it cannot dial anything but the configured destination."""
    return HTMLResponse(PAGE)


# ----------------------------------------------------------------- monitor

MONITOR = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Call monitor</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#0d0d0f;color:#e8e8ea;
   font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
 header{padding:.9rem 1.1rem;border-bottom:1px solid #26262b;display:flex;
   gap:.8rem;align-items:baseline;flex-wrap:wrap;background:#0d0d0f}
 h1{font-size:1rem;margin:0}
 .dim{color:#8b8b93}
 main{padding:1.1rem;max-width:56rem;margin:0 auto;display:grid;gap:1.1rem}
 section{border:1px solid #26262b;border-radius:.6rem;overflow:hidden}
 h2{font:600 .78rem/1 inherit;letter-spacing:.06em;text-transform:uppercase;
   margin:0;padding:.65rem .9rem;background:#17171a;color:#a1a1aa}
 .body{padding:.8rem .9rem;display:grid;gap:.45rem}
 .turn{display:grid;grid-template-columns:3.2rem 1fr;gap:.6rem;align-items:start}
 .who{font:600 .72rem/1.6 inherit;letter-spacing:.04em}
 .agent .who{color:#7dd3fc} .lead .who{color:#86efac}
 .pill{display:inline-block;padding:.12rem .5rem;border-radius:999px;
   font:600 .72rem/1.6 inherit;border:1px solid #3f3f46}
 .hot{background:#7f1d1d;border-color:#b91c1c} .warm{background:#78350f;border-color:#b45309}
 .cold{background:#1e3a5f;border-color:#1d4ed8}
 .sent{color:#86efac} .failed{color:#fca5a5} .pending,.sending{color:#fcd34d}
 table{width:100%;border-collapse:collapse} td{padding:.28rem 0;vertical-align:top}
 td:first-child{color:#8b8b93;width:11rem;white-space:nowrap}
 code{font:.82rem ui-monospace,SFMono-Regular,Menlo,monospace;color:#c4b5fd}
 a{color:#7dd3fc}
</style>
<header>
  <h1>Call monitor</h1>
  <span class=dim id=meta>loading…</span>
  <span class=dim style="margin-left:auto">refreshing every 3s</span>
</header>
<main id=out></main>
<script>
const q=new URLSearchParams(location.search), pinned=q.get('id');
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const t=s=>s?new Date(s).toLocaleTimeString():'';
async function tick(){
  try{
    let id=pinned;
    if(!id){ const l=await (await fetch('/calls?limit=1')).json();
             if(!l.calls.length){document.getElementById('meta').textContent='no calls yet';return;}
             id=l.calls[0].id; }
    const d=await (await fetch('/calls/'+id)).json(), c=d.call;
    document.getElementById('meta').innerHTML=
      `<code>${esc(c.id.slice(0,8))}</code> · ${esc(c.status)}`+
      (c.ended_reason?` · ${esc(c.ended_reason)}`:'')+` · to ${esc(c.destination)}`;
    const cls=d.classification;
    const turns=d.turns.map(x=>`<div class="turn ${x.role==='user'?'lead':'agent'}">
        <span class=who>${x.role==='user'?'LEAD':'AGENT'}</span>
        <span>${esc(x.text)}</span></div>`).join('')||'<span class=dim>no turns yet</span>';
    const slots=Object.entries(d.slots).map(([k,v])=>
        `<tr><td>${esc(k)}</td><td>${esc(v.value)}${v.raw_quote?
         `<br><span class=dim>“${esc(v.raw_quote)}”</span>`:''}</td></tr>`).join('')
        ||'<tr><td colspan=2 class=dim>nothing extracted yet</td></tr>';
    const acts=d.actions.map(a=>`<tr><td>${esc(a.type)}</td>
        <td><span class="${esc(a.status)}">${esc(a.status)}</span>
        <span class=dim>· ${esc(a.trigger_source||'')} · ${t(a.sent_at||a.requested_at)}</span>
        ${a.error?`<br><span class=failed>${esc(a.error.slice(0,120))}</span>`:''}</td></tr>`)
        .join('')||'<tr><td colspan=2 class=dim>none fired</td></tr>';
    const cbs=d.callbacks.map(b=>`<tr><td>${esc(b.status)}</td>
        <td>${esc(b.resolved_at_utc)} <span class=dim>· “${esc(b.spoken_phrase)}”
        · ${esc(b.resolution_rule)}</span></td></tr>`).join('')
        ||'<tr><td colspan=2 class=dim>none booked</td></tr>';
    const hist=d.classification_history.map(h=>
        `<span class="pill ${esc(h.label)}">${esc(h.label)}</span>`).join(' ');
    document.getElementById('out').innerHTML=`
      <section><h2>Intent ${cls?`— now <span class="pill ${esc(cls.label)}">${esc(cls.label)}</span>`:''}</h2>
        <div class=body>${hist||'<span class=dim>not classified yet</span>'}
        ${cls&&cls.evidence_quote?`<div class=dim>“${esc(cls.evidence_quote)}”</div>`:''}</div></section>
      <section><h2>Transcript (${d.turns.length})</h2><div class=body>${turns}</div></section>
      <section><h2>Extracted</h2><div class=body><table>${slots}</table></div></section>
      <section><h2>Messages &amp; actions</h2><div class=body><table>${acts}</table></div></section>
      <section><h2>Callbacks</h2><div class=body><table>${cbs}</table></div></section>`;
  }catch(e){ document.getElementById('meta').textContent='error: '+e.message; }
}
tick(); setInterval(tick,3000);
</script>"""


@app.get("/monitor", response_class=HTMLResponse)
def monitor():
    """Live view of the newest call - or a specific one with ?id=<call_id>.

    Polls the same read routes the API exposes, so it can never show anything
    the data does not. Exists because a phone call is impossible to debug after
    the fact from logs alone: this shows the transcript, how the intent read
    evolved, what was extracted, and exactly which messages fired and when.
    """
    return HTMLResponse(MONITOR)


# ----------------------------------------------------------------- reads

@app.get("/calls")
def list_calls(limit: int = 20):
    return {"calls": db.list_calls(limit)}


@app.get("/calls/{call_id}")
def get_call(call_id: str):
    call = db.get_call(call_id)
    if not call:
        raise HTTPException(404, "no such call")
    return {
        "call": call,
        "turns": db.get_turns(call_id),
        "slots": db.get_slots(call_id),
        "coverage": understanding.coverage(call_id),
        "classification": db.latest_classification(call_id),
        "classification_history": db.classification_history(call_id),
        "actions": db.get_actions(call_id),
        "callbacks": db.get_callbacks(call_id),
        # App deep links, carrier-fault flags, escalation, calendar checks.
        **views.detail_extras(call_id),
    }


@app.get("/calls/{call_id}/transcript")
def get_transcript(call_id: str):
    if not db.get_call(call_id):
        raise HTTPException(404, "no such call")
    return {"call_id": call_id, "transcript": db.transcript_text(call_id)}


@app.get("/api/config")
def ui_config():
    """What the web UI needs to start a call. The Vapi PUBLIC key is designed to
    live in a browser; the destination is shown masked, never in full."""
    return {
        "vapi_public_key": settings.vapi_public_key,
        "assistant_id": settings.vapi_assistant_id if settings.vapi_public_key else "",
        "destination_masked": views.mask(settings.allowed_destination),
        "can_place_calls": not settings.missing(),
        "missing": settings.missing(),
    }


@app.get("/api/calls")
def call_summaries(limit: int = 30):
    """Calls with intent, per-app action status and fault flags, for the Calls list."""
    return {"calls": [views.call_summary(c) for c in db.list_calls(min(limit, 100))]}


@app.get("/api/calls/by-provider/{provider_call_id}")
def call_by_provider(provider_call_id: str):
    """A web call knows only Vapi's id; the row appears on its first webhook."""
    return {"call_id": db.call_id_for_provider(provider_call_id)}


# ----------------------------------------------------------------- webhook

@app.post("/vapi/webhook")
async def vapi_webhook(request: Request, background: BackgroundTasks,
                       x_vapi_secret: str = Header(default="")):
    """Single ingress from Vapi. Acknowledges immediately.

    Only `tool-calls` needs a synchronous answer, and even that one just claims
    an action and returns - it never waits on a send.
    """
    if settings.webhook_secret and x_vapi_secret != settings.webhook_secret:
        raise HTTPException(401, "bad webhook secret")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body is not JSON")

    message = body.get("message") or {}
    msg_type = message.get("type") or "unknown"
    provider_call_id = (message.get("call") or {}).get("id")
    call_id = db.call_id_for_provider(provider_call_id)

    # Adopt calls we did not place ourselves.
    #
    # agent/agent.py dials Vapi directly, so no `calls` row exists and every
    # webhook used to fall into the "unknown call" branch: transcripts were never
    # classified and tool calls were answered with no result at all. The call
    # worked, the tool fired, and nothing happened - the failure was invisible
    # from both ends. Creating the row on first sighting makes the backend work
    # regardless of who placed the call: the CLI, POST /calls, or the dashboard.
    if call_id is None and provider_call_id:
        customer = ((message.get("call") or {}).get("customer") or {})
        destination = customer.get("number") or settings.allowed_destination
        call_id = db.create_call(destination, assistant_id=settings.vapi_assistant_id)
        if db.attach_provider_id(call_id, provider_call_id):
            db.add_event(call_id, "call.adopted",
                         {"provider_call_id": provider_call_id, "via": msg_type})
            log.info("adopted call %s (provider %s) first seen via %s",
                     call_id, provider_call_id, msg_type)
        else:
            # Two webhooks raced us to it; the other one won.
            call_id = db.call_id_for_provider(provider_call_id)

    db.add_event(call_id, f"vapi.{msg_type}", body)

    if call_id is None:
        log.warning("webhook %s for unresolvable provider call %s", msg_type, provider_call_id)
        if msg_type == "tool-calls":
            # Must still answer with a results array, or the agent hangs on
            # "one moment" until Vapi times out.
            return _unknown_tool_results(message)
        return {"received": True, "known_call": False}

    if msg_type == "tool-calls":
        return await _handle_tool_calls(call_id, message, background)

    background.add_task(_process, call_id, msg_type, message)
    return {"received": True}


def _tool_calls_in(message):
    return message.get("toolCallList") or message.get("toolCalls") or []


def _json_args(raw):
    """Vapi sends tool arguments as an object, but some model providers hand it
    back as a JSON string. Accept both rather than lose a booking to a type."""
    import json
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _unknown_tool_results(message):
    """Answer every tool call even when we cannot attribute it to a call."""
    return JSONResponse({"results": [
        {"toolCallId": tc.get("id") or tc.get("toolCallId"),
         "result": "Not available right now."}
        for tc in _tool_calls_in(message)
    ]})


async def _handle_tool_calls(call_id, message, background):
    """Answer the model's tool call in milliseconds.

    The primary mid-call action path. It must return fast enough that the agent
    can keep talking - so it claims the action and returns a confirmation string
    the model can speak, without waiting for anything to actually be sent.
    """
    results = []
    for tc in message.get("toolCallList") or message.get("toolCalls") or []:
        tool_id = tc.get("id") or tc.get("toolCallId")
        name = ((tc.get("function") or {}).get("name")) or tc.get("name") or ""
        args = (tc.get("function") or {}).get("arguments") or tc.get("arguments") or {}

        if name in ("send_details_now", "send_whatsapp_now"):
            if settings.whatsapp_enabled:
                actions.dispatch(call_id, "whatsapp_hot", payload={"args": args},
                                 trigger_source="tool_call", background=background)
                said = "Sent. Tell them it is on its way to their WhatsApp now."
            else:
                # WhatsApp is off, so nothing is sent and the agent must not say it
                # was. An assistant deployed before the tool was removed can still
                # call it; the read itself still drives Slack and HubSpot.
                said = ("Nothing is sent from this call. Tell them our team will put the "
                        "details together, and offer a quick callback to walk them through it.")
            results.append({"toolCallId": tool_id, "result": said})
        elif name == "schedule_callback":
            # Resolved synchronously and deterministically - it is milliseconds,
            # and the resolved time has to come back in THIS result so the agent
            # can say it aloud while the person is still on the line.
            if isinstance(args, str):
                args = _json_args(args)
            phrase = (args or {}).get("when") or (args or {}).get("time") or ""
            # OFF the event loop. book() is three synchronous SQLite writes, and
            # this handler runs while hundreds of transcript webhooks and the
            # extraction threads are contending for the same file. Blocking here
            # delayed the result long enough that Vapi's silence timer killed a
            # live call AFTER the callback had already been booked - the booking
            # survived, the agent never got to say it aloud, which is the half
            # the evaluator can actually hear.
            spoken = await asyncio.to_thread(callbacks.book, call_id, phrase)
            # Confirm it in writing, on the booking rather than at call end, so
            # the lead has the time in their hand and we have visible proof the
            # scheduler ran. Idempotent, so restating a time does not re-send.
            if spoken.startswith("Booked"):
                if settings.whatsapp_enabled:
                    actions.dispatch(call_id, "callback_confirm",
                                     trigger_source="callback_booked",
                                     background=background)
                # Calendar event, CRM stage and the team's Slack message all
                # follow from the booking - in the background, after this result
                # has already gone back to the agent.
                booked = callfacts.active_callback(call_id)
                if booked:
                    integrations.on_callback_booked(call_id, booked["id"],
                                                    background=background)
            results.append({"toolCallId": tool_id, "result": spoken})
        else:
            log.warning("unknown tool call %r on call %s", name, call_id)
            results.append({"toolCallId": tool_id, "result": "Unknown tool."})

    return JSONResponse({"results": results})


async def _process(call_id, msg_type, message):
    """Background half of the webhook. Slow work is fine here."""
    try:
        if msg_type == "status-update":
            status = message.get("status")
            fields = {"status": status}
            if status == "in-progress":
                fields["answered_at"] = db.utc_now()
            if status == "ended":
                fields["ended_at"] = db.utc_now()
                fields["ended_reason"] = message.get("endedReason")
            db.update_call(call_id, **fields)

        elif msg_type == "transcript":
            # Partials churn; only finals are worth persisting or reasoning over.
            if (message.get("transcriptType") or "").lower() != "final":
                return
            role = "user" if message.get("role") == "user" else "assistant"
            text = (message.get("transcript") or "").strip()
            if not text:
                return
            seq = db.add_turn(call_id, role, text)
            if seq is not None:
                await understanding.on_turn(call_id, role, text, seq)

        elif msg_type == "end-of-call-report":
            call = message.get("call") or {}
            artifact = message.get("artifact") or {}
            # Same path the reconciliation sweeper uses, so a call finalized by
            # a late webhook and one finalized by the sweeper end up identical.
            await reconcile.finalize(
                call_id,
                ended_reason=message.get("endedReason"),
                started_at=call.get("startedAt"),
                recording_url=artifact.get("recordingUrl") or message.get("recordingUrl"),
                summary=message.get("summary"),
                messages=artifact.get("messages") or message.get("messages") or [],
                source="webhook",
            )

    except Exception:
        log.exception("failed processing %s for call %s", msg_type, call_id)
