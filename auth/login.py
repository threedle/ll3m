import base64
import hashlib
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs
import requests

from config.loader import get_cognito_config
from .token_store import TOKEN_PATH


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _generate_pkce() -> tuple[str, str]:
    verifier = _b64url(os.urandom(40))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _open_browser(url: str) -> None:
    import webbrowser
    webbrowser.open(url, new=2)


def login_via_pkce() -> None:
    cfg = get_cognito_config()
    domain = cfg["domain"]
    client_id = cfg["client_id"]
    redirect_uri = cfg["redirect_uri"]
    scopes = cfg.get("scopes", ["openid", "email", "phone", "profile"])

    state = _b64url(os.urandom(16))
    verifier, challenge = _generate_pkce()

    code_holder = {"code": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urlparse(self.path)
            if q.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = parse_qs(q.query)
            code = params.get("code", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Login received. You can close this tab.")
            code_holder["code"] = code

        def log_message(self, fmt, *args):  # silence
            return

    # Start local server
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    httpd = HTTPServer((host, port), Handler)
    print("[Login] Starting local callback server on", f"{host}:{port}")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    # Build auth URL
    auth_url = (
        f"{domain}/oauth2/authorize?" +
        urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
    )
    print("[Login] Opening browser to Cognito Hosted UI...")
    _open_browser(auth_url)

    # Wait for code
    for _ in range(600):  # up to ~5 minutes
        if code_holder["code"]:
            break
        time.sleep(0.5)
    # Try to shutdown server without blocking forever
    try:
        stopper = threading.Thread(target=httpd.shutdown, daemon=True)
        stopper.start()
        stopper.join(timeout=2.0)
    except Exception:
        pass

    code = code_holder["code"]
    if not code:
        raise RuntimeError("Login timed out or was canceled")
    print("[Login] Authorization code received. Exchanging for tokens...")

    # Exchange for tokens
    token_url = f"{domain}/oauth2/token"
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    try:
        resp = requests.post(token_url, data=data, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print("[Login] Token exchange failed:", e)
        try:
            print("[Login] Response:", getattr(e, 'response', None).text)
        except Exception:
            pass
        raise
    tokens = resp.json()

    os.makedirs(TOKEN_PATH.parent, exist_ok=True)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(resp.text)
    print(f"[Login] Saved tokens to {TOKEN_PATH}")


def logout_local() -> None:
    try:
        if TOKEN_PATH.exists():
            TOKEN_PATH.unlink()
            print("Local tokens cleared.")
    except Exception:
        pass


