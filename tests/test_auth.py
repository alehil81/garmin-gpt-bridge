import base64
import copy
import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import RLock
from unittest.mock import MagicMock, Mock, patch

import requests
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import garmin_session as auth
from app import garmin_client as data
from app.main import app
from app.token_store import TokenStore, TokenStoreError


def tokens(expiry=None, marker="initial"):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    jwt = ".".join((part({"alg": "RS256"}), part({
        "exp": expiry if expiry is not None else time.time() + 86400,
        "client_id": "test-client", "jti": marker,
    }), "test-signature"))
    return {"di_token": jwt, "di_refresh_token": "refresh-" + marker, "di_client_id": "test-client"}


def response(status=200, retry_after="", renewed=None):
    result = requests.Response()
    result.status_code = status
    result.url = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
    result.headers["Retry-After"] = retry_after
    renewed = renewed or tokens(marker="renewed")
    result._content = json.dumps({"access_token": renewed["di_token"],
                                 "refresh_token": renewed["di_refresh_token"]}).encode()
    return result


class MemoryStore:
    def __init__(self, initial):
        self.record = {"schema": 1, "tokens": initial, "failure": {}}
        self.lock = RLock()
        self.fail_commit = False

    def read(self):
        return copy.deepcopy(self.record)

    @contextmanager
    def locked(self):
        with self.lock:
            working = self.read()
            yield working
            if self.fail_commit:
                self.fail_commit = False
                raise TokenStoreError("Simulated failed commit")
            self.record = copy.deepcopy(working)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.store = MemoryStore(tokens(self.now - 1))
        self.manager = auth.SessionManager(self.store)
        self.manager._new_client()
        patches = [
            patch.dict(os.environ, {"API_KEY": "test-key"}, clear=True),
            patch.object(auth, "_MANAGER", self.manager),
            patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network request")),
            patch("garminconnect.Garmin.login", side_effect=AssertionError("Password login forbidden")),
            patch("garminconnect.client.Client.login", side_effect=AssertionError("Password login forbidden")),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_expired_token_renews_once_and_is_saved(self):
        renewed = tokens(marker="new")
        with patch.object(self.manager._client.client.cs, "post", return_value=response(renewed=renewed)) as post:
            self.manager.ensure_session()
            self.manager.ensure_session()
        post.assert_called_once()
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.store.record["tokens"], renewed)
        self.assertEqual(self.store.record["failure"], {})

    def test_valid_token_does_not_renew(self):
        self.store.record["tokens"] = tokens()
        with patch.object(self.manager._client.client.cs, "post") as post:
            self.manager.ensure_session()
        post.assert_not_called()

    def test_new_process_restores_renewed_token_without_environment_seeds(self):
        with patch.object(self.manager._client.client.cs, "post", return_value=response()):
            self.manager.ensure_session()
        restarted = auth.SessionManager(self.store)
        restarted.ensure_session()
        self.assertEqual(json.loads(restarted._client.client.dumps()), self.store.record["tokens"])

    def test_two_instances_and_threads_share_one_renewal(self):
        other = auth.SessionManager(self.store)
        with patch("requests.sessions.Session.post", return_value=response()) as post:
            managers = [self.manager, other] * 4
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda item: item.ensure_session(), managers))
        post.assert_called_once()

    def test_429_cooldown_survives_new_process(self):
        with patch.object(self.manager._client.client.cs, "post", return_value=response(429)) as post:
            for _ in range(3):
                with self.assertRaises(HTTPException) as caught:
                    self.manager.ensure_session()
                self.assertEqual(caught.exception.status_code, 503)
        post.assert_called_once()
        restarted = auth.SessionManager(self.store)
        with self.assertRaises(HTTPException):
            restarted.ensure_session()
        self.assertEqual(restarted.status()["last_http_status"], 429)
        self.assertGreater(restarted.status()["retry_after_seconds"], 0)

    def test_retry_after_is_honored_and_recovery_clears_failure(self):
        with patch.object(self.manager._client.client.cs, "post", return_value=response(429, "7200")):
            with self.assertRaises(HTTPException):
                self.manager.ensure_session()
        self.assertGreaterEqual(self.manager.status()["retry_after_seconds"], 7199)
        renewed = tokens(self.now + 20000, "later")
        with patch.object(auth.time, "time", return_value=self.now + 7201), \
                patch.object(self.manager._client.client.cs, "post", return_value=response(renewed=renewed)):
            self.manager.ensure_session()
        self.assertEqual(self.store.record["failure"], {})

    def test_revoked_refresh_stops_until_relinked(self):
        with patch.object(self.manager._client.client.cs, "post", return_value=response(400)) as post:
            with self.assertRaises(HTTPException):
                self.manager.ensure_session()
            with patch.object(auth.time, "time", return_value=self.now + 999999):
                with self.assertRaises(HTTPException):
                    self.manager.ensure_session()
        post.assert_called_once()
        self.assertTrue(self.manager.status()["reauthentication_required"])

    def test_commit_failure_retries_storage_not_garmin(self):
        self.store.fail_commit = True
        renewed = tokens(marker="rotation")
        with patch.object(self.manager._client.client.cs, "post", return_value=response(renewed=renewed)) as post:
            with self.assertRaises(TokenStoreError):
                self.manager.ensure_session()
            self.manager.ensure_session()
        post.assert_called_once()
        self.assertEqual(self.store.record["tokens"], renewed)

    def test_newer_remote_session_is_not_overwritten_by_pending_save(self):
        self.store.fail_commit = True
        with patch.object(self.manager._client.client.cs, "post", return_value=response()):
            with self.assertRaises(TokenStoreError):
                self.manager.ensure_session()
        newer = tokens(marker="other-process")
        self.store.record["tokens"] = newer
        self.manager.ensure_session()
        self.assertEqual(self.store.record["tokens"], newer)

    def test_bad_or_missing_tokens_never_trigger_password_login(self):
        for invalid in (None, {}, {"di_token": "not-json"}, tokens(float("inf")), tokens(True)):
            self.store.record["tokens"] = invalid
            with self.assertRaises(HTTPException):
                self.manager.ensure_session()

    def test_invalid_renewal_is_paused(self):
        invalid = tokens(self.now - 600)
        with patch.object(self.manager._client.client.cs, "post", return_value=response(renewed=invalid)) as post:
            for _ in range(2):
                with self.assertRaises(HTTPException):
                    self.manager.ensure_session()
        post.assert_called_once()

    def test_api_rate_limit_is_durable(self):
        self.store.record["tokens"] = tokens()
        self.manager.ensure_session()
        hook = self.manager._client.client._api_session.hooks["response"][0]
        with self.assertRaises(HTTPException):
            hook(response(429, "3600"))
        with self.assertRaises(HTTPException):
            auth.SessionManager(self.store).ensure_session()
        self.assertEqual(self.store.record["failure"]["stage"], "garmin_api")

    def test_repeated_unauthorized_api_response_requires_relink(self):
        self.store.record["tokens"] = tokens()
        hook = self.manager._client.client._api_session.hooks["response"][0]
        hook(response(401))
        with self.assertRaises(HTTPException):
            hook(response(401))
        self.assertTrue(self.store.record["failure"]["reauthentication_required"])

    def test_profile_is_loaded_only_once_without_library_login(self):
        self.store.record["tokens"] = tokens()
        with patch.object(self.manager._client, "connectapi", return_value={"displayName": "test", "fullName": "Test"}) as get:
            self.manager.get_client()
            self.manager.get_client()
        get.assert_called_once_with("/userprofile-service/socialProfile")

    def test_status_is_safe_and_does_not_contact_garmin(self):
        result = self.manager.status()
        self.assertEqual(result["auth_backend"], "garmin_di")
        self.assertTrue(result["access_token_expired"])
        self.assertNotIn("refresh-initial", json.dumps(result))
        self.assertFalse(result["password_login_attempted"])

    def test_storage_outage_fails_closed(self):
        with patch.object(self.store, "locked", side_effect=TokenStoreError("Unavailable")):
            with self.assertRaises(HTTPException) as caught:
                auth.get_garmin_client()
        self.assertEqual(caught.exception.status_code, 503)

    def test_routes_remain_protected(self):
        client = TestClient(app)
        self.assertEqual(client.get("/health").json(), {"ok": True})
        self.assertEqual(client.get("/version").json(), {"version": "1.1.0"})
        self.assertEqual(client.get("/debug_env").status_code, 401)
        self.assertEqual(client.get("/daily_summary?date=2026-09-11").status_code, 401)
        result = client.get("/debug_env", headers={"Authorization": "Bearer test-key"})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["garmin_auth"]["token_store_available"])
        self.assertNotIn("refresh-initial", result.text)

    def test_all_data_routes_preserve_auth_pause_and_retry_after(self):
        paused = HTTPException(503, "Garmin is paused", headers={"Retry-After": "1800"})
        with patch.object(data, "_get_garmin_client", side_effect=paused), \
                patch("app.main._get_garmin_client", side_effect=paused):
            client = TestClient(app)
            for url in ("/daily_summary?date=2026-09-11", "/sleep_summary?day=2026-09-11",
                        "/debug_sleep?day=2026-09-11", "/sleep_range?start=2026-09-11&end=2026-09-12",
                        "/activities?start=2026-09-11&end=2026-09-12"):
                result = client.get(url, headers={"Authorization": "Bearer test-key"})
                self.assertEqual(result.status_code, 503, url)
                self.assertEqual(result.headers["Retry-After"], "1800", url)

    def test_pause_during_optional_sleep_fetch_is_not_swallowed(self):
        client = Mock()
        client.get_stats_and_body.return_value = {"restingHeartRate": 54}
        client.get_sleep_data.side_effect = HTTPException(503, "Paused", headers={"Retry-After": "1800"})
        with patch.object(data, "_get_garmin_client", return_value=client):
            result = TestClient(app).get("/daily_summary?date=2026-09-11", headers={"Authorization": "Bearer test-key"})
        self.assertEqual(result.status_code, 503)

    def test_existing_longer_cooldown_is_not_shortened(self):
        self.store.record["failure"] = {"stage": "garmin_api", "http_status": 429, "retry_at": self.now + 7200}
        hook = self.manager._client.client._api_session.hooks["response"][0]
        with self.assertRaises(HTTPException):
            hook(response(429, "30"))
        self.assertGreaterEqual(self.manager.status()["retry_after_seconds"], 7199)

    def test_pool_settings_are_applied_inside_transaction(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        connection.__enter__.return_value = connection
        store = TokenStore("unused", Fernet.generate_key().decode())
        with patch("app.token_store.psycopg.connect", return_value=connection) as connect:
            with store.locked():
                pass
        self.assertNotIn("options", connect.call_args.kwargs)
        self.assertEqual(connect.call_args.kwargs["sslmode"], "verify-full")
        queries = [call.args[0] for call in connection.execute.call_args_list]
        self.assertTrue(any("SET LOCAL lock_timeout" in query for query in queries))
        self.assertTrue(any("pg_advisory_xact_lock" in query for query in queries))

    def test_daily_summary_contract_is_unchanged(self):
        client = Mock()
        client.get_stats_and_body.return_value = {"restingHeartRate": 54}
        client.get_sleep_data.return_value = {}
        with patch.object(data, "_get_garmin_client", return_value=client):
            result = TestClient(app).get("/daily_summary?date=2026-09-11",
                                        headers={"Authorization": "Bearer test-key"})
        self.assertEqual(result.json(), {"date": "2026-09-11", "steps": None, "calories": None,
                                         "restingHr": 54.0, "hrv": None})

    def test_ciphertext_hides_tokens_and_requires_correct_key(self):
        store = TokenStore("unused", Fernet.generate_key().decode())
        encoded = store.encode(self.store.record)
        self.assertNotIn("refresh-initial", encoded)
        self.assertNotIn("test-client", encoded)
        self.assertEqual(store.decode(encoded), self.store.record)
        other = TokenStore("unused", Fernet.generate_key().decode())
        with self.assertRaises(InvalidToken):
            other.decode(encoded)

    def test_encryption_and_database_config_are_required(self):
        with self.assertRaises(TokenStoreError):
            TokenStore.from_env()
        with self.assertRaises(TokenStoreError):
            TokenStore("unused", "invalid")


if __name__ == "__main__":
    unittest.main()
