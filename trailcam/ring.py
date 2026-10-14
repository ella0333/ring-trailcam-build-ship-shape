"""Ring Partner API client (api.amazonvision.com).

Covers what the trail cam needs: tokens, devices, device location, clip download,
snapshot download, webhook signatures and the account linking calls. Endpoints and
retry rules follow https://developer.amazon.com/docs/ring/api-documentation.html.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import random
import threading
import time
from dataclasses import dataclass

import httpx

from .config import settings

log = logging.getLogger(__name__)

TOKENS_FILE = settings.data_dir / "tokens.json"
RETRYABLE_MEDIA = (416, 425)
RETRY_SCHEDULE_S = (5, 10, 20, 30)


class RingError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Ring API {status}: {body[:300]}")
        self.status = status
        self.body = body


# ---------------------------------------------------------------- webhooks


def verify_signature(raw_body: bytes, signature: str | None, key: str | None = None) -> bool:
    """X-Signature is 'sha256=<hex HMAC-SHA256 of the raw body>'."""
    key = key if key is not None else settings.hmac_key
    if not key or not signature:
        return False
    expected = hmac.new(key.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


def link_nonce(time_param: str, account_id: str, key: str | None = None) -> str:
    """Account linking nonce: URL-safe base64 (no padding) of HMAC-SHA256('<time>:<account_id>')."""
    key = key if key is not None else settings.hmac_key
    mac = hmac.new(key.encode(), f"{time_param}:{account_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


# ---------------------------------------------------------------- tokens


def _load_tokens() -> dict:
    try:
        return json.loads(TOKENS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_tokens(data: dict) -> None:
    TOKENS_FILE.write_text(json.dumps(data, indent=1))


class TokenStore:
    """The Ring credentials for the current mode, kept in data/tokens.json.

    Simulation: a Ring Developer Playground token, pasted on the dashboard. It lasts about
    30 minutes and can't be refreshed.
    Camera: the access and refresh tokens from account linking. The access token lasts about
    4 hours and is refreshed at oauth.ring.com, which also rotates the refresh token.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def status(self) -> dict:
        t = _load_tokens()
        if settings.mode == "simulation":
            token = (t.get("playground") or {}).get("token")
            expires = token_expiry(token)
            valid = bool(token) and (expires is None or expires > time.time())
            return {"connected": valid, "expires_at": expires if token else None, "expired": bool(token) and not valid}
        cam = t.get("camera") or {}
        return {"connected": bool(cam.get("refresh_token")), "account_id": cam.get("account_id"),
                "unclaimed": len(t.get("unclaimed", []))}

    def set_playground(self, token: str) -> None:
        with self._lock:
            t = _load_tokens()
            t["playground"] = {"token": token.strip(), "saved_at": int(time.time())}
            _save_tokens(t)

    def set_camera(self, refresh: str, access: str, expires_in: int | None, account_id: str) -> None:
        with self._lock:
            t = _load_tokens()
            t["camera"] = {"refresh_token": refresh, "access_token": access, "account_id": account_id,
                           "expires_at": int(time.time()) + int(expires_in or 14400) - 60}
            _save_tokens(t)

    def get(self) -> str:
        with self._lock:
            t = _load_tokens()
            if settings.mode == "simulation":
                token = (t.get("playground") or {}).get("token")
                if not token:
                    raise RingError(401, "no Playground token yet, paste one on the dashboard")
                return token
            cam = t.get("camera") or {}
            if not cam.get("refresh_token"):
                raise RingError(401, "no Ring account linked yet, finish camera setup on the dashboard")
            if cam.get("access_token") and time.time() < cam.get("expires_at", 0):
                return cam["access_token"]
            data = oauth_token({"grant_type": "refresh_token", "refresh_token": cam["refresh_token"]})
            cam.update(refresh_token=data.get("refresh_token", cam["refresh_token"]),
                       access_token=data["access_token"],
                       expires_at=int(time.time()) + int(data.get("expires_in", 14400)) - 60)
            t["camera"] = cam
            _save_tokens(t)
            return cam["access_token"]


def token_expiry(token: str | None) -> int | None:
    """Expiry (epoch seconds) from a JWT's exp claim. Not a validity check, just for display."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (AttributeError, IndexError, ValueError, KeyError, TypeError):
        return None


def oauth_token(form: dict) -> dict:
    """POST to Ring's OAuth token endpoint (authorization_code or refresh_token grant)."""
    form = {**form, "client_id": settings.client_id, "client_secret": settings.client_secret}
    r = httpx.post(settings.oauth_url, data=form, timeout=20)
    if r.status_code != 200:
        raise RingError(r.status_code, r.text)
    return r.json()


tokens = TokenStore()


# ---------------------------------------------------------------- API


@dataclass
class Clip:
    data: bytes
    partial: bool
    actual_ms: int | None
    media_ts: int | None


class RingClient:
    def __init__(self, base: str | None = None, token_source: TokenStore | None = None) -> None:
        self.base = (base or settings.api_base).rstrip("/")
        self.tokens = token_source or tokens
        # httpx drops the Authorization header when a redirect leaves the origin, which
        # is what we want for Ring's pre-signed media URLs.
        self.http = httpx.Client(timeout=60, follow_redirects=True)
        self._location_cache: dict[str, dict] = {}

    def _headers(self, token: str | None = None) -> dict:
        return {"Authorization": f"Bearer {token or self.tokens.get()}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, token: str | None = None, **kw) -> httpx.Response:
        for attempt in range(4):
            r = self.http.request(method, self.base + path, headers=self._headers(token), **kw)
            if r.status_code in (429, 503) and attempt < 3:
                wait = float(r.headers.get("Retry-After") or 2 ** attempt)
                time.sleep(min(wait, 30))
                continue
            return r
        return r

    def _json(self, method: str, path: str, token: str | None = None, **kw) -> dict:
        r = self._request(method, path, token, **kw)
        if r.status_code >= 400:
            raise RingError(r.status_code, r.text)
        return r.json() if r.content else {}

    # devices
    def devices(self) -> list[dict]:
        return self._json("GET", "/v1/devices?include=status,location").get("data", [])

    def devices_with(self, token: str) -> list[dict]:
        """Device list for a token that is not saved yet, to check it before saving."""
        return self._json("GET", "/v1/devices", token).get("data", [])

    def me(self, token: str | None = None) -> dict:
        return self._json("GET", "/v1/users/me", token)

    def location(self, device_id: str) -> dict:
        """Coarse location, {'country': 'US', 'state': 'GA'}; cached, it rarely changes."""
        if device_id not in self._location_cache:
            try:
                data = self._json("GET", f"/v1/devices/{device_id}/location")
                self._location_cache[device_id] = (data.get("data") or {}).get("attributes") or {}
            except RingError as e:
                log.warning("no location for %s: %s", device_id, e)
                self._location_cache[device_id] = {}
        return self._location_cache[device_id]

    def configuration(self, device_id: str) -> dict:
        data = self._json("GET", f"/v1/devices/{device_id}/configurations")
        return (data.get("data") or {}).get("attributes") or {}

    def is_encrypted(self, device_id: str) -> bool:
        """TAKE-encrypted video can't be read. Ring says to check before processing video and
        to treat it as expected. e2e_encryption.enabled is a JSON boolean, not "on"/"off"."""
        try:
            return (self.configuration(device_id).get("e2e_encryption") or {}).get("enabled") is True
        except RingError as e:
            log.warning("no configuration for %s: %s", device_id, e)
            return False

    # event history
    def history(self, device_id: str, event_types: str | None = None) -> list[dict]:
        """Newest first. event_types takes Ring's filters, e.g. 'motion' or 'motion.animal'."""
        params = {"event_types": event_types} if event_types else None
        return self._json("GET", f"/v1/history/devices/{device_id}/events", params=params).get("data", [])

    # Event History's documented subtype filters, plus the webhook's package_delivery value.
    # Most specific first: an event can match more than one, and the Playground tags its
    # synthetic events "human" alongside the real one.
    SUBTYPES = ("package_delivery", "vehicle", "animal", "human", "other_motion")

    def motion_tags(self, device_id: str, event_id: str) -> list[str]:
        """Every subtype Ring gives this event. History withholds the subtype, so it can only
        be read by repeating the query once per event_types filter."""
        tags = []
        for sub in self.SUBTYPES:
            try:
                found = self.history(device_id, f"motion.{sub}")
            except RingError as e:  # a filter Ring doesn't accept shouldn't stop the others
                log.info("Event History filter motion.%s not accepted: %s", sub, e.status)
                continue
            if any(e.get("id") == event_id for e in found):
                tags.append(sub)
        return tags

    def motion_subtype(self, device_id: str, event_id: str) -> str | None:
        """The most specific subtype Ring gives this event."""
        tags = self.motion_tags(device_id, event_id)
        return tags[0] if tags else None

    # media
    def download_clip(self, device_id: str, ts_ms: int, duration_ms: int, component_id: str | None = None) -> Clip:
        body: dict = {"timestamp": ts_ms, "duration": duration_ms}
        if component_id is not None:
            body["components"] = [{"component_id": component_id}]
        r = self._request("POST", f"/v1/devices/{device_id}/media/video/download", json=body)
        if r.status_code not in (200, 206):
            raise RingError(r.status_code, r.text)
        length = r.headers.get("X-Media-Length")
        media_ts = r.headers.get("X-Media-Timestamp")
        return Clip(r.content, r.status_code == 206, int(length) if length else None,
                    int(media_ts) if media_ts else None)

    def download_clip_after_event(self, device_id: str, ts_ms: int, duration_ms: int,
                                  component_id: str | None = None) -> Clip | None:
        """Ring's documented flow for a motion webhook: wait until [T, T+D) is in the past,
        wait 5 s more for processing, then retry 416/425 unchanged until 2 minutes after
        the range ends. Returns None when the clip never becomes available."""
        range_end = ts_ms / 1000 + duration_ms / 1000
        wait = range_end + settings.media_wait_s - time.time()
        if wait > 0:
            time.sleep(wait)
        deadline = range_end + settings.media_deadline_s
        attempt = 0
        while True:
            try:
                return self.download_clip(device_id, ts_ms, duration_ms, component_id)
            except RingError as e:
                if e.status not in RETRYABLE_MEDIA:
                    raise
                log.info("clip not ready (%s), retrying", e.status)
            delay = RETRY_SCHEDULE_S[min(attempt, len(RETRY_SCHEDULE_S) - 1)]
            delay += random.uniform(0, delay * 0.1)
            attempt += 1
            if time.time() + delay > deadline:
                return None
            time.sleep(delay)

    def download_snapshot(self, device_id: str, ts_ms: int, component_id: str | None = None) -> bytes:
        # Full resolution keeps Ring's fixed-size watermark small relative to the frame.
        body: dict = {"type": "at_timestamp", "timestamp": ts_ms,
                      "image_options": {"format": "jpeg", "resolution": {"width": 1920, "height": 1080}}}
        if component_id is not None:
            body["components"] = [{"component_id": component_id}]
        r = self._request("POST", f"/v1/devices/{device_id}/media/image/download", json=body)
        if r.status_code != 200:
            raise RingError(r.status_code, r.text)
        return r.content

    # account linking
    def confirm_link(self, token: str, nonce: str, account_identifier: str) -> dict:
        return self._json("POST", "/v1/accounts/me/app-integrations", token,
                          json={"account_identifier": account_identifier, "nonce": nonce})

    def complete_link(self, token: str) -> dict:
        return self._json("PATCH", "/v1/accounts/me/app-integrations", token, json={"status": "completed"})
