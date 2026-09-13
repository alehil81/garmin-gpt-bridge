"""Import an already authorized Garmin DI session without printing its secrets."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.garmin_session import token_expiry
from app.token_store import TokenStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("token_file", type=Path, help="Private garmin_tokens.json file from a successful local sign-in")
    args = parser.parse_args()
    try:
        source = json.loads(args.token_file.read_text())
        tokens = {name: source[name] for name in ("di_token", "di_refresh_token", "di_client_id")}
        expiry = token_expiry(tokens)
        if expiry is None or not all(isinstance(v, str) and v for v in tokens.values()):
            raise ValueError("Invalid session")
        with TokenStore.from_env().locked() as record:
            record["tokens"] = tokens
            record["failure"] = {}
    except Exception as error:
        raise SystemExit(f"Session import failed: {type(error).__name__}. Secret details withheld.") from None
    print("Session stored encrypted. No Garmin request was sent; no redeployment is needed.")


if __name__ == "__main__":
    main()
