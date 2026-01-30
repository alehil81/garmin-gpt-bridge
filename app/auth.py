import os
from fastapi import Header, HTTPException

def require_bearer_token(authorization: str | None = Header(default=None)) -> None:
    """
    Requires: Authorization: Bearer <API_KEY>
    """
    api_key = (os.getenv("API_KEY") or "").strip()

    # Remove accidental surrounding quotes if pasted into Render
    if (
        (api_key.startswith('"') and api_key.endswith('"'))
        or (api_key.startswith("'") and api_key.endswith("'"))
    ):
        api_key = api_key[1:-1].strip()

    if not api_key:
        raise HTTPException(status_code=500, detail="Server misconfiguration")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    token = authorization.removeprefix("Bearer ").strip()
    if token != api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
