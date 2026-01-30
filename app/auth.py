import os
from fastapi import Header, HTTPException

def require_bearer_token(authorization: str | None = Header(default=None)) -> None:
    """
    Requires: Authorization: Bearer <API_KEY>
    Set env var: API_KEY
    """
    api_key = os.getenv("API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Server missing API_KEY env var")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = authorization.removeprefix("Bearer ").strip()
    if token != api_key:
        raise HTTPException(status_code=403, detail="Invalid token")
