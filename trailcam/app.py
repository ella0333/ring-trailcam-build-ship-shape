"""Ring Trail Cam web service.

Run:  uvicorn trailcam.app:app --port 8000
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import store, tunnel
from .config import settings
from .pipeline import CLIPS, THUMBS, Pipeline
from .poller import PlaygroundPoller
from .ring import TOKENS_FILE, RingError, link_nonce, oauth_token, tokens, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("trailcam")

STATIC = Path(__file__).parent / "static"
LINK_WINDOW_S = 600

app = FastAPI(title="Ring Trail Cam")
app.mount("/media/clips", StaticFiles(directory=CLIPS), name="clips")
app.mount("/media/thumbs", StaticFiles(directory=THUMBS), name="thumbs")
pipeline = Pipeline()
poller = PlaygroundPoller(pipeline)


# Paths reachable through the tunnel. Everything else is local only.
PUBLIC_PATHS = ("/webhooks/ring", "/ring/token-exchange", "/ring/link")


@app.middleware("http")
async def tunnel_guard(request: Request, call_next):
    if "cf-connecting-ip" in request.headers and not request.url.path.startswith(PUBLIC_PATHS):
        return JSONResponse({"error": "not found"}, status_code=404)
    return await call_next(request)


@app.on_event("startup")
def _startup() -> None:
    pipeline.warm_up()
    poller.start()
    if settings.mode == "camera" and settings.client_id:
        tunnel.start()


def _local_only(request: Request) -> None:
    """Settings can only be changed from the computer the app runs on. Requests through the
    Cloudflare tunnel carry cf-connecting-ip, so they are refused even though they arrive locally."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost") or "cf-connecting-ip" in request.headers:
        raise HTTPException(403, "settings can only be changed on the computer running Ring Trail Cam")


# ---------------------------------------------------------------- Ring webhook


@app.post("/webhooks/ring")
async def ring_webhook(request: Request, x_signature: str | None = Header(default=None)):
    raw = await request.body()  # verify the exact bytes Ring signed, before parsing
    if not verify_signature(raw, x_signature):
        log.warning("rejected webhook with bad signature")
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    try:
        payload = json.loads(raw)
        meta, data = payload["meta"], payload["data"]
        attrs = data.get("attributes") or {}
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "malformed payload"}, status_code=400)

    event_type = data.get("type")
    if event_type != "motion_detected":
        log.info("ignoring %s", event_type)
        return {"status": "ignored"}

    components = attrs.get("component_ids") or []
    status = pipeline.intake(
        request_id=meta.get("request_id") or data.get("id"), event_id=data.get("id", ""),
        device_id=attrs.get("source", ""), account_id=meta.get("account_id", ""),
        ts_ms=int(attrs.get("timestamp") or time.time() * 1000),
        sub_type=attrs.get("sub_type") or "motion",
        component_id=components[0] if components else None, source="ring",
    )
    return {"status": status}


# ---------------------------------------------------------------- dashboard


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state():
    return {
        "sightings": store.sightings(),
        "species": store.species_counts(),
        "events": store.events(40),
        "totals": store.event_totals(),
        "mode": settings.mode,
        "simulation_available": settings.simulation_available,
        "ring": tokens.status(),
        "camera": {
            "keys_saved": bool(settings.client_id and settings.client_secret and settings.hmac_key),
            "tunnel": tunnel.state["status"],
            "tunnel_error": tunnel.state["error"],
            "public_url": settings.public_url,
        },
    }


@app.post("/api/mode")
def set_mode(request: Request, mode: str = Form(...)):
    """Choose simulation (Ring Developer Playground) or camera (your Ring camera)."""
    _local_only(request)
    if mode not in ("simulation", "camera") or (mode == "simulation" and not settings.simulation_available):
        raise HTTPException(400, "unknown mode")
    settings.save(mode=mode)
    if mode == "camera" and settings.client_id:
        tunnel.start()
    return {"ok": True}


@app.post("/api/camera")
def set_camera_keys(request: Request, client_id: str = Form(...), client_secret: str = Form(...),
                    hmac_key: str = Form(...)):
    """Save the Ring app keys from the developer console, then start the tunnel."""
    _local_only(request)
    values = {"client_id": client_id.strip(), "client_secret": client_secret.strip(), "hmac_key": hmac_key.strip()}
    if not all(values.values()):
        raise HTTPException(400, "all three keys are needed")
    settings.save(**values)
    tunnel.start()
    return {"ok": True}


@app.post("/api/token")
def set_token(request: Request, token: str = Form(...)):
    """Save a Ring Developer Playground token (they last about 30 minutes) after checking it with Ring."""
    _local_only(request)
    token = token.strip()
    try:
        found = pipeline.ring.devices_with(token)
    except RingError as e:
        msg = "Ring did not accept this token. Generate a new one." if e.status == 401 else str(e)
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    tokens.set_playground(token)
    names = [(d.get("attributes") or {}).get("name") or d["id"] for d in found]
    return {"ok": True, "devices": names}


@app.get("/api/devices")
def devices():
    """Connection test: lists the Ring devices the current token can see."""
    try:
        out = []
        for d in pipeline.ring.devices():
            a = d.get("attributes") or {}
            out.append({"id": d["id"], "name": a.get("name"), "location": pipeline.ring.location(d["id"])})
        return {"ok": True, "devices": out}
    except RingError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@app.post("/api/simulate")
def simulate(request: Request, count: int = Form(3)):
    """Send random clips from the simulation pool through the pipeline (send_clips.py)."""
    _local_only(request)
    if not settings.simulation_available:
        raise HTTPException(404, "no simulation clips in this version")
    return {"queued": pipeline.simulate(max(1, min(count, 10)))}


@app.post("/api/sightings/{sighting_id}/feedback")
def feedback(request: Request, sighting_id: int, verdict: str = Form(...)):
    """Confirm or reject a label, as Ring's CV guidelines recommend for a best-effort app."""
    _local_only(request)
    if verdict not in ("confirmed", "rejected", "none"):
        raise HTTPException(400, "verdict must be confirmed, rejected or none")
    store.set_feedback(sighting_id, None if verdict == "none" else verdict)
    return {"ok": True}


@app.post("/api/reset")
def reset(request: Request):
    _local_only(request)
    store.clear()
    for d in (CLIPS, THUMBS):
        for f in d.iterdir():
            f.unlink()
    return {"ok": True}


# ---------------------------------------------------------------- Ring account linking (one-way)


def _tokens_file() -> dict:
    try:
        return json.loads(TOKENS_FILE.read_text())
    except (OSError, ValueError):
        return {}


@app.post("/ring/token-exchange")
async def token_exchange(request: Request):
    """Ring sends an authorization code here (server to server) when a user enables the app.
    The code lives 60 seconds, so it is exchanged at once and the tokens are held as
    'unclaimed' until the user signs in on the account link page."""
    raw = await request.body()
    code = request.query_params.get("code")
    if not code:
        try:
            code = json.loads(raw).get("code")
        except ValueError:
            form = await request.form()
            code = form.get("code")
    log.info("token exchange request, content-type=%s, has code=%s",
             request.headers.get("content-type"), bool(code))
    if not code:
        return JSONResponse({"error": "missing code"}, status_code=400)
    try:
        data = oauth_token({"grant_type": "authorization_code", "code": code})
        account_id = pipeline.ring.me(token=data["access_token"]).get("data", {}).get("id", "")
    except RingError as e:
        log.error("token exchange failed: %s", e)
        return JSONResponse({"error": "exchange failed"}, status_code=500)
    t = _tokens_file()
    t.setdefault("unclaimed", []).append({
        "account_id": account_id, "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"), "expires_in": data.get("expires_in"),
        "received_at": int(time.time()),
    })
    TOKENS_FILE.write_text(json.dumps(t, indent=1))
    return {"status": "ok"}


LINK_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Link Ring Trail Cam</title>
<style>body{{font:16px system-ui;background:#10140f;color:#e8eadf;display:grid;place-items:center;min-height:100vh;margin:0;padding:16px}}
form{{background:#1a2018;padding:28px;border-radius:12px;max-width:360px;width:100%}}
input{{width:100%;padding:10px;margin:8px 0 16px;border-radius:6px;border:1px solid #3a4535;background:#10140f;color:inherit;box-sizing:border-box}}
button{{width:100%;padding:11px;border:0;border-radius:6px;background:#7fc47a;color:#10140f;font-weight:600}}
p{{color:#a9b09f}}</style></head><body><form method="post">
<h2>Ring Trail Cam</h2><p>{message}</p>
<input type="hidden" name="nonce" value="{nonce}"><input type="hidden" name="time" value="{time}">
<label>Email<input name="email" type="email" required></label>
<label>Password<input name="password" type="password" required></label>
<button>Connect my Ring account</button></form></body></html>"""


ACCOUNT_FILE = settings.data_dir / "account.json"


def check_account(email: str, password: str) -> bool:
    email = email.strip().lower()
    try:
        acct = json.loads(ACCOUNT_FILE.read_text())
    except (OSError, ValueError):
        salt = secrets.token_hex(16)
        ACCOUNT_FILE.write_text(json.dumps({"email": email, "salt": salt, "hash": _hash(password, salt)}))
        return True
    return email == acct["email"] and hmac.compare_digest(_hash(password, acct["salt"]), acct["hash"])


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


@app.get("/ring/link/done", response_class=HTMLResponse)
def link_done():
    """Default Redirect URL: where Ring sends people after linking."""
    return LINK_DONE


LINK_DONE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Ring Trail Cam</title>
<style>body{font:16px system-ui;background:#10140f;color:#e8eadf;display:grid;place-items:center;min-height:100vh;margin:0;padding:16px}
a{color:#7fc47a}</style></head><body><p>Your Ring account is connected. Go back to Ring Trail Cam at
<a href="http://localhost:8000">http://localhost:8000</a>.</p></body></html>"""


@app.get("/ring/link", response_class=HTMLResponse)
def link_page(request: Request):
    q = request.query_params
    msg = ("Sign in to finish connecting your Ring cameras." if ACCOUNT_FILE.exists()
           else "Create an account to finish connecting your Ring cameras.")
    return LINK_PAGE.format(message=msg,
                            nonce=q.get("nonce", ""), time=q.get("time", ""))


@app.post("/ring/link", response_class=HTMLResponse)
def link_submit(nonce: str = Form(...), time: str = Form(...), email: str = Form(...), password: str = Form(...)):
    import time as _t

    def page(msg: str) -> str:
        return LINK_PAGE.format(message=msg, nonce=nonce, time=time)

    # Ring requires a sign-in before a token is claimed. The first link creates the account.
    if not check_account(email, password):
        return page("Wrong email or password.")
    age = (_t.time() * 1000 - int(time)) / 1000
    if age > LINK_WINDOW_S or age < 0:
        return page("This link has expired. Start again from Connect in the Ring developer console.")
    t = _tokens_file()
    match = next((u for u in t.get("unclaimed", [])
                  if hmac.compare_digest(link_nonce(time, u["account_id"]), nonce)), None)
    if match is None:
        return page("No pending Ring connection matches this link.")
    name, _, domain = email.partition("@")
    masked = f"{name[:1]}***{name[-1:]}@{domain}"
    try:
        pipeline.ring.confirm_link(match["access_token"], nonce, masked)
        pipeline.ring.complete_link(match["access_token"])
    except RingError as e:
        log.error("account link failed: %s", e)
        return page("Ring did not accept the link. Start again from Connect in the Ring developer console.")
    t["unclaimed"] = [u for u in t["unclaimed"] if u is not match]
    TOKENS_FILE.write_text(json.dumps(t, indent=1))
    tokens.set_camera(match["refresh_token"], match["access_token"], match.get("expires_in"),
                      match["account_id"])
    return RedirectResponse("http://localhost:8000/?linked=1", status_code=303)
