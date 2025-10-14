import json
import os
from pathlib import Path
from typing import Dict


TOKEN_PATH = Path(os.path.expanduser("~/.ll3m/token.json"))


def load_access_token() -> str | None:
    # Env override for simplicity
    env_tok = os.environ.get("LL3M_ACCESS_TOKEN")
    if env_tok:
        return env_tok
    try:
        if TOKEN_PATH.exists():
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("access_token")
    except Exception:
        return None
    return None


def get_auth_headers() -> Dict[str, str]:
    tok = load_access_token()
    return ({"Authorization": f"Bearer {tok}"} if tok else {})


