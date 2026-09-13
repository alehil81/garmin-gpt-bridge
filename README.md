# Garmin GPT Bridge

The 1.1 authentication path uses Garmin Connect DI tokens and encrypted PostgreSQL
storage. It does not use Garth's retired OAuth1 exchange or perform password login
inside API requests. Endpoint URLs and the GPT's bearer API key are unchanged.

## Deployment

Use Python 3.14.3 and install `requirements.txt`. The Render blueprint remains on
the free plan. Configure these private environment variables:

- `API_KEY`: the existing key used by the GPT Action.
- `GARMIN_TOKEN_DATABASE_URL`: a TLS PostgreSQL connection for the dedicated token database.
- `GARMIN_TOKEN_ENCRYPTION_KEY`: a Fernet key generated once and retained separately from the database.

The database needs this table, owned by an administrative setup role:

```sql
CREATE SCHEMA garmin_bridge;
CREATE TABLE garmin_bridge.sessions (
    id text PRIMARY KEY,
    payload text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

Give the application role only `USAGE` on that schema and `SELECT, INSERT, UPDATE`
on the table. The encryption key is never sent to PostgreSQL. The database contains
encrypted authentication state, not health data. Retain the encryption key securely:
losing it makes the stored session unreadable. Do not put either secret in Git.

After one successful local Garmin sign-in creates `garmin_tokens.json`, import it
with the two storage environment variables configured in the local process:

```sh
python scripts/import_di_session.py /private/path/garmin_tokens.json
```

The old `OAUTH1_B64`, `OAUTH2_B64`, and their aliases are ignored by version 1.1.
The legacy `scripts/print_render_oauth_env.py` is not a setup step for this version.
`GARMIN_EMAIL` and `GARMIN_PASSWORD` are not needed on Render.

## Recovery

Renewal is serialized with a database transaction lock, and rotated tokens are saved
before the next Garmin API request. Cold starts restore the latest database session;
there are no stale environment-token seeds to overwrite it. HTTP 429 pauses are also
stored in the database, with Garmin's longer Retry-After value honored.

Health and version checks do not contact Garmin. The protected `/debug_env` endpoint
reports storage availability, token expiration and renewal state without returning
credentials. A storage outage fails closed instead of triggering credential login.

If Garmin rejects a refresh token with HTTP 400/401/403, automatic attempts stop and
`reauthentication_required` is reported. Relink locally and import the new session;
no redeployment is needed. Database-write failures retain rotated tokens in process
memory for a subsequent save attempt. A simultaneous process loss and database
outage can still require relinking. Garmin changes, revoked sessions and service
outages cannot be prevented by this bridge.

## Tests

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

The unit tests block real HTTP calls and exercise refresh, restart recovery,
concurrent renewal, rate limits, revoked tokens, encryption, storage failures,
and existing endpoint contracts.
