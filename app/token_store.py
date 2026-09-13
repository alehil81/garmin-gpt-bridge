"""Encrypted session persistence; the database never receives the encryption key."""

import copy
import json
import os
from contextlib import contextmanager

import certifi
import psycopg
from cryptography.fernet import Fernet, InvalidToken


class TokenStoreError(Exception):
    pass


class TokenStore:
    def __init__(self, database_url: str, encryption_key: str, session_id: str = "garmin"):
        self._database_url = database_url
        self._session_id = session_id
        try:
            self._cipher = Fernet(encryption_key.encode())
        except (ValueError, TypeError):
            raise TokenStoreError("Token encryption is not configured correctly") from None

    @classmethod
    def from_env(cls):
        url = os.getenv("GARMIN_TOKEN_DATABASE_URL", "").strip()
        key = os.getenv("GARMIN_TOKEN_ENCRYPTION_KEY", "").strip()
        if not url or not key:
            raise TokenStoreError("Persistent Garmin token storage is not configured")
        return cls(url, key)

    def _connect(self):
        return psycopg.connect(
            self._database_url,
            connect_timeout=10,
            sslmode="verify-full",
            sslrootcert=certifi.where(),
        )

    def encode(self, record: dict) -> str:
        return self._cipher.encrypt(json.dumps(record, allow_nan=False).encode()).decode()

    def decode(self, payload: str) -> dict:
        record = json.loads(self._cipher.decrypt(payload.encode()))
        if not isinstance(record, dict) or record.get("schema") != 1:
            raise TokenStoreError("Unsupported encrypted session format")
        if not isinstance(record.get("failure", {}), dict):
            raise TokenStoreError("Invalid encrypted session state")
        return record

    def _read(self, connection):
        row = connection.execute(
            "SELECT payload FROM garmin_bridge.sessions WHERE id = %s", (self._session_id,)
        ).fetchone()
        return self.decode(row[0]) if row else {"schema": 1, "tokens": None, "failure": {}}

    def read(self) -> dict:
        try:
            with self._connect() as connection:
                connection.execute("SET LOCAL statement_timeout = '15s'")
                return self._read(connection)
        except (psycopg.Error, OSError, InvalidToken, ValueError, TypeError):
            raise TokenStoreError("Encrypted token storage could not be read") from None

    @contextmanager
    def locked(self):
        try:
            with self._connect() as connection:
                connection.execute("SET LOCAL statement_timeout = '15s'")
                connection.execute("SET LOCAL lock_timeout = '10s'")
                # The transaction-scoped lock works with Neon's pooled connections.
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("garmin_bridge:" + self._session_id,),
                )
                record = self._read(connection)
                before = copy.deepcopy(record)
                yield record
                if record != before:
                    connection.execute(
                        "INSERT INTO garmin_bridge.sessions (id, payload) VALUES (%s, %s) "
                        "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()",
                        (self._session_id, self.encode(record)),
                    )
        except (psycopg.Error, OSError, InvalidToken, ValueError, TypeError):
            raise TokenStoreError("Encrypted token storage is unavailable; Garmin login was not retried") from None
