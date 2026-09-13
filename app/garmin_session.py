"""Token-only Garmin authentication with durable refresh and rate-limit state."""

import base64
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import RLock

from fastapi import HTTPException
from garminconnect import Garmin

from .token_store import TokenStore, TokenStoreError


def token_expiry(tokens: dict | None) -> float | None:
    try:
        parts = tokens["di_token"].split(".")
        header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
        if header.get("alg") in (None, "none"):
            return None
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        value = payload["exp"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        return float(value)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return None


def fingerprint(tokens) -> str:
    return hashlib.sha256(json.dumps(tokens, sort_keys=True).encode()).hexdigest()


def retry_seconds(value: str) -> int:
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, math.ceil((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 0


class SessionManager:
    def __init__(self, store: TokenStore):
        self.store = store
        self._lock = RLock()
        self._client = None
        self._profile_ready = False
        self._pending_tokens = None
        self._pending_base = None
        self._pending_refresh_at = None
        self._last_record = {}
        self._last_refresh_response = None
        self._unauthorized_responses = 0

    def _new_client(self):
        client = Garmin()
        transport = client.client
        original_request = transport._run_request

        def refresh_post(url, **kwargs):
            if url != "https://diauth.garmin.com/di-oauth2-service/oauth/token":
                raise RuntimeError("Unexpected authentication destination")
            kwargs["allow_redirects"] = False
            kwargs["timeout"] = 20
            response = transport.cs.post(url, **kwargs)
            self._last_refresh_response = response
            response.raise_for_status()
            return response

        def request(method, path, **kwargs):
            with self._lock:
                try:
                    self.ensure_session()
                    self._unauthorized_responses = 0
                    return original_request(method, path, **kwargs)
                except TokenStoreError:
                    raise HTTPException(503, "Encrypted Garmin session storage is unavailable; no password login was attempted.") from None

        def response_hook(response, *args, **kwargs):
            if response.status_code == 401:
                self._unauthorized_responses += 1
            if response.status_code == 429 or (response.status_code == 401 and self._unauthorized_responses >= 2):
                failure = self._failure("garmin_api", response)
                with self.store.locked() as record:
                    existing = record.get("failure", {})
                    if existing.get("reauthentication_required") or existing.get("retry_at", 0) > failure["retry_at"]:
                        failure = existing
                    record["failure"] = failure
                self._last_record["failure"] = failure
                raise self._paused(failure)

        # The bridge owns retries/persistence; the library supplies Garmin's DI flow.
        transport._http_post = refresh_post
        transport._refresh_session = lambda: self.ensure_session(force=True)
        transport._run_request = request
        transport._api_session.hooks["response"].append(response_hook)
        self._client = client

    @staticmethod
    def _failure(stage, response=None):
        status = response.status_code if response is not None else None
        header = response.headers.get("Retry-After", "") if response is not None else ""
        delay = max(1800, retry_seconds(header)) if status == 429 else 60
        return {"stage": stage, "http_status": status, "retry_at": time.time() + delay,
                "reauthentication_required": status in (400, 401, 403)}

    @staticmethod
    def _paused(failure):
        delay = max(1, math.ceil(failure.get("retry_at", 0) - time.time()))
        return HTTPException(
            503,
            f"Garmin requests are paused after {failure.get('stage', 'authentication')} "
            f"(HTTP {failure.get('http_status') or 'unavailable'}). Retry after {delay} seconds. "
            "Password login was not attempted.",
            headers={"Retry-After": str(delay)},
        )

    def ensure_session(self, force=False):
        with self._lock:
            if self._client is None:
                self._new_client()
            transport = self._client.client
            previous = fingerprint(json.loads(transport.dumps()))
            failure = None
            with self.store.locked() as record:
                failure = record.get("failure", {})
                if failure.get("reauthentication_required"):
                    raise HTTPException(503, "Garmin rejected the saved session. Relink Garmin once; automatic password login is disabled.")
                if failure.get("retry_at", 0) > time.time():
                    self._last_record = dict(record)
                    raise self._paused(failure)
                tokens = record.get("tokens")
                if self._pending_tokens is not None and fingerprint(tokens) == self._pending_base:
                    record["tokens"] = tokens = self._pending_tokens
                    record["failure"] = {}
                    record["refresh_count"] = record.get("refresh_count", 0) + 1
                    record["last_refresh_at"] = self._pending_refresh_at
                expiry = token_expiry(tokens)
                if (expiry is None or not tokens.get("di_refresh_token") or not tokens.get("di_client_id")):
                    raise HTTPException(503, "A current Garmin DI session must be linked once. Password login was not attempted.")
                transport.loads(json.dumps(tokens))
                changed = fingerprint(tokens) != previous
                if expiry <= time.time() + 900 or (force and not changed):
                    self._last_refresh_response = None
                    try:
                        transport._refresh_di_token()
                        renewed = json.loads(transport.dumps())
                        renewed_expiry = token_expiry(renewed)
                        if renewed_expiry is None or renewed_expiry <= time.time():
                            raise ValueError("Invalid renewed token")
                    except Exception:
                        failure = self._failure("di_token_refresh", self._last_refresh_response)
                        record["failure"] = failure
                    else:
                        # Retain a rotated token in memory if the database commit fails.
                        self._pending_base = fingerprint(tokens)
                        self._pending_tokens = renewed
                        self._pending_refresh_at = time.time()
                        record["tokens"] = renewed
                        record["failure"] = {}
                        record["refresh_count"] = record.get("refresh_count", 0) + 1
                        record["last_refresh_at"] = self._pending_refresh_at
                        failure = None
                else:
                    failure = None
                self._last_record = dict(record)
            self._pending_tokens = None
            self._pending_base = None
            self._pending_refresh_at = None
            if failure:
                raise self._paused(failure)

    def get_client(self):
        with self._lock:
            self.ensure_session()
            if not self._profile_ready:
                # Avoid the library login's password fallbacks and profile retry loop.
                profile = self._client.connectapi("/userprofile-service/socialProfile")
                name = profile.get("displayName") if isinstance(profile, dict) else None
                if not isinstance(name, str) or not name:
                    raise HTTPException(502, "Garmin returned an incomplete profile; no login retry was attempted.")
                self._client.display_name = name
                self._client.full_name = profile.get("fullName", "")
                self._profile_ready = True
            return self._client

    def status(self):
        record = self.store.read()
        self._last_record = record
        failure = record.get("failure", {})
        expiry = token_expiry(record.get("tokens"))
        return {
            "auth_backend": "garmin_di",
            "token_store": "encrypted_postgres",
            "token_store_available": True,
            "client_cached": self._profile_ready,
            "session_present": record.get("tokens") is not None,
            "last_failure_stage": failure.get("stage"),
            "last_http_status": failure.get("http_status"),
            "retry_after_seconds": max(0, math.ceil(failure.get("retry_at", 0) - time.time())),
            "access_token_expires_at": expiry,
            "access_token_expired": expiry <= time.time() if expiry is not None else None,
            "reauthentication_required": failure.get("reauthentication_required", False),
            "password_login_attempted": False,
            "refresh_count": record.get("refresh_count", 0),
            "last_refresh_at": record.get("last_refresh_at"),
        }


_MANAGER = None
_MANAGER_LOCK = RLock()


def _manager():
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = SessionManager(TokenStore.from_env())
        return _MANAGER


def get_garmin_client():
    try:
        return _manager().get_client()
    except TokenStoreError:
        raise HTTPException(503, "Encrypted Garmin session storage is unavailable. No password login was attempted.") from None


def garmin_auth_status():
    try:
        return _manager().status()
    except TokenStoreError:
        return {"auth_backend": "garmin_di", "token_store": "encrypted_postgres",
                "token_store_available": False, "password_login_attempted": False}
