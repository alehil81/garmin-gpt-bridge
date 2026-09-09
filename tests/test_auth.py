import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient
from garminconnect import GarminConnectTooManyRequestsError
from garth.exc import GarthHTTPError

from app import garmin_client as gc
from app.main import app


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.oauth1 = {
            "oauth_token": "test-oauth1", "oauth_token_secret": "test-secret",
            "domain": "garmin.com",
        }
        self.oauth2 = {
            "scope": "test", "jti": "test", "token_type": "Bearer",
            "access_token": "test-access", "refresh_token": "test-refresh",
            "expires_in": 3600, "expires_at": int(time.time()) + 3600,
            "refresh_token_expires_in": 86400,
            "refresh_token_expires_at": int(time.time()) + 86400,
        }
        env = {
            "API_KEY": "test-key",
            "OAUTH1_B64": self.encode(self.oauth1),
            "OAUTH2_B64": self.encode(self.oauth2),
        }
        patches = [
            patch.dict(os.environ, env, clear=True),
            patch.object(gc, "TOKENSTORE_DIR", str(self.directory)),
            patch.object(gc, "OAUTH1_PATH", str(self.directory / "oauth1_token.json")),
            patch.object(gc, "OAUTH2_PATH", str(self.directory / "oauth2_token.json")),
            patch.object(gc, "_GARMIN_CLIENT", None),
            patch.object(gc, "_AUTH_FAILURE", {}),
            patch.object(gc, "_PASSWORD_LOGIN_ATTEMPTED", False),
            patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network request")),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def encode(value):
        return base64.b64encode(json.dumps(value).encode()).decode()

    @staticmethod
    def rate_error(retry_after=""):
        response = requests.Response()
        response.status_code = 429
        response.url = "https://connectapi.garmin.com/oauth-service/oauth/exchange/user/2.0?secret=never-log"
        response.headers["Retry-After"] = retry_after
        inner = requests.HTTPError("sensitive upstream message", response=response)
        error = GarminConnectTooManyRequestsError("rate limit")
        error.__cause__ = GarthHTTPError("request failed", inner)
        return error

    def test_repeated_calls_and_restart_do_not_retry_during_cooldown(self):
        with patch.object(gc, "_load_client_from_tokenstore", side_effect=self.rate_error()) as load, \
                patch.object(gc, "_login_client_with_credentials") as password:
            for attempt in range(3):
                if attempt == 2:
                    gc._AUTH_FAILURE = {}  # Simulate a process restart with the same disk.
                with self.assertRaises(HTTPException) as caught:
                    gc._get_garmin_client()
                self.assertEqual(caught.exception.status_code, 503)
                self.assertGreater(int(caught.exception.headers["Retry-After"]), 0)
            load.assert_called_once()
            password.assert_not_called()
        status = gc.garmin_auth_status()
        self.assertEqual(status["last_failure_stage"], "oauth2_refresh")
        self.assertEqual(status["last_http_status"], 429)
        self.assertNotIn("never-log", gc._failure_path().read_text())
        self.assertNotIn("sensitive", gc._failure_path().read_text())

    def test_retry_after_and_successful_recovery(self):
        with patch.object(gc.time, "time", return_value=1000):
            error = gc._auth_error(self.rate_error("7200"))
            self.assertEqual(error.headers["Retry-After"], "7200")
        client = Mock()
        with patch.object(gc.time, "time", return_value=8201), \
                patch.object(gc, "_load_client_from_tokenstore", return_value=client) as load:
            self.assertIs(gc._get_garmin_client(), client)
            self.assertIs(gc._get_garmin_client(), client)
            load.assert_called_once()
        self.assertFalse(gc._failure_path().exists())
        self.assertEqual(gc._AUTH_FAILURE, {})

    def test_env_restore_preserves_refreshed_disk_tokens(self):
        gc._write_tokens_from_env_to_disk()
        new_token = {**self.oauth2, "access_token": "refreshed"}
        Path(gc.OAUTH2_PATH).write_text(json.dumps(new_token))
        gc._write_tokens_from_env_to_disk()
        self.assertEqual(json.loads(Path(gc.OAUTH2_PATH).read_text()), new_token)
        os.environ["OAUTH2_B64"] = self.encode({**self.oauth2, "access_token": "new-env"})
        gc._write_tokens_from_env_to_disk()
        self.assertEqual(json.loads(Path(gc.OAUTH2_PATH).read_text())["access_token"], "new-env")
        self.assertEqual(Path(gc.OAUTH2_PATH).stat().st_mode & 0o777, 0o600)

    def test_actual_garth_refresh_is_saved_by_response_hook(self):
        gc._write_tokens_from_env_to_disk()
        client = gc._new_client()
        client.garth.load(gc.TOKENSTORE_DIR)
        client.garth.oauth2_token.access_token = "refreshed-after-startup"
        for hook in client.garth.sess.hooks["response"]:
            hook(requests.Response())
        gc._write_tokens_from_env_to_disk()
        self.assertEqual(json.loads(Path(gc.OAUTH2_PATH).read_text())["access_token"], "refreshed-after-startup")

    def test_invalid_or_partial_env_does_not_use_password(self):
        for value in ("", "not-base64", self.encode(["wrong shape"])):
            os.environ["OAUTH2_B64"] = value
            with patch.object(gc, "_login_client_with_credentials") as password:
                with self.assertRaises(HTTPException):
                    gc._get_garmin_client()
                password.assert_not_called()

    def test_debug_env_is_protected_and_makes_no_garmin_request(self):
        client = TestClient(app)
        self.assertEqual(client.get("/debug_env").status_code, 401)
        result = client.get("/debug_env", headers={"Authorization": "Bearer test-key"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["garmin_auth"]["retry_after_seconds"], 0)
        self.assertNotIn("test-access", result.text)
        self.assertEqual(client.get("/version").json(), {"version": "1.0.1"})


if __name__ == "__main__":
    unittest.main()
