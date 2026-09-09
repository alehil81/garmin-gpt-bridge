#!/usr/bin/env python3
import argparse
import base64
from pathlib import Path


def encode_file(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print Render env values from garth OAuth token files."
    )
    parser.add_argument(
        "tokenstore",
        nargs="?",
        default="~/.garth",
        help="Directory containing oauth1_token.json and oauth2_token.json.",
    )
    args = parser.parse_args()

    tokenstore = Path(args.tokenstore).expanduser()
    oauth1 = tokenstore / "oauth1_token.json"
    oauth2 = tokenstore / "oauth2_token.json"

    missing = [str(path) for path in (oauth1, oauth2) if not path.exists()]
    if missing:
        raise SystemExit(f"Missing token file(s): {', '.join(missing)}")

    print(f"OAUTH1_B64={encode_file(oauth1)}")
    print(f"OAUTH2_B64={encode_file(oauth2)}")


if __name__ == "__main__":
    main()
