"""Autenticazione Spotify: Authorization Code Flow con refresh token salvato su file."""
import base64
import json
import logging
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

SCOPES = [
    "user-follow-read",
    "user-top-read",
    "user-library-read",
    "user-read-recently-played",
    "playlist-modify-private",
    "playlist-modify-public",
]


class TokenManager:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, token_path: Path):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_path = Path(token_path)
        self._token = self._load()

    # ---------- file token ----------
    def _load(self):
        if self.token_path.exists():
            return json.loads(self.token_path.read_text(encoding="utf-8"))
        env_refresh = os.getenv("SPOTIFY_REFRESH_TOKEN", "").strip()
        if env_refresh:  # usato più avanti da GitHub Actions
            return {"access_token": None, "refresh_token": env_refresh, "expires_at": 0}
        return None

    def _save(self) -> None:
        self.token_path.write_text(json.dumps(self._token), encoding="utf-8")

    def _store(self, payload: dict) -> None:
        old_refresh = (self._token or {}).get("refresh_token")
        new_refresh = payload.get("refresh_token")
        if new_refresh and old_refresh and new_refresh != old_refresh:
            log.info("Spotify ha emesso un nuovo refresh token (salvato)")
        self._token = {
            "access_token": payload["access_token"],
            "refresh_token": new_refresh or old_refresh,
            "expires_at": time.time() + int(payload.get("expires_in", 3600)) - 60,
            "scope": payload.get("scope", ""),
        }
        self._save()

    def _basic_header(self) -> dict:
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    # ---------- login (una volta sola) ----------
    def authorize(self, timeout_seconds: int = 180) -> None:
        state = secrets.token_urlsafe(16)
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
        }
        url = f"{AUTH_URL}?{urlencode(params)}"
        parsed = urlparse(self.redirect_uri)
        result: dict = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                query = parse_qs(urlparse(self.path).query)
                if "code" in query or "error" in query:
                    result.update({key: values[0] for key, values in query.items()})
                    text = (
                        "Autorizzazione completata. Puoi chiudere questa scheda."
                        if "code" in query
                        else "Autorizzazione non riuscita. Torna al terminale."
                    )
                else:
                    text = "In attesa dell'autorizzazione..."
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(text.encode("utf-8"))

            def log_message(self, *args):
                pass

        server = HTTPServer((parsed.hostname, parsed.port), Handler)
        server.timeout = 5
        print("\nApro il browser per l'autorizzazione Spotify.")
        print("Se non si apre da solo, copia questo indirizzo nel browser:\n")
        print(url + "\n")
        webbrowser.open(url)

        deadline = time.time() + timeout_seconds
        try:
            while time.time() < deadline and "code" not in result and "error" not in result:
                server.handle_request()
        finally:
            server.server_close()

        if "error" in result:
            raise RuntimeError(f"Autorizzazione rifiutata da Spotify: {result['error']}")
        if "code" not in result:
            raise RuntimeError("Tempo scaduto: nessuna autorizzazione ricevuta.")
        if result.get("state") != state:
            raise RuntimeError("Parametro 'state' non valido: autorizzazione annullata per sicurezza.")

        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": result["code"],
                "redirect_uri": self.redirect_uri,
            },
            headers=self._basic_header(),
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Scambio del codice fallito ({resp.status_code}): {resp.text[:300]}")
        self._store(resp.json())

    # ---------- uso quotidiano ----------
    def get_access_token(self, force_refresh: bool = False) -> str:
        if not self._token or not self._token.get("refresh_token"):
            raise RuntimeError("Nessun token Spotify trovato. Esegui prima: python authorize.py")
        if (
            force_refresh
            or not self._token.get("access_token")
            or time.time() >= self._token.get("expires_at", 0)
        ):
            self._refresh()
        return self._token["access_token"]

    def _refresh(self) -> None:
        resp = requests.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self._token["refresh_token"]},
            headers=self._basic_header(),
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Rinnovo del token Spotify fallito ({resp.status_code}): {resp.text[:300]}. "
                "Rilancia: python authorize.py"
            )
        self._store(resp.json())