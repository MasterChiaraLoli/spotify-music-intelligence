"""Client HTTP per la Spotify Web API con retry, rate limit e paginazione."""
import logging
import time

import requests

from .auth import TokenManager

log = logging.getLogger(__name__)

API_BASE = "https://api.spotify.com/v1"
MAX_RETRY_AFTER = 120  # oltre questi secondi non aspettiamo: meglio fallire con un errore chiaro


class SpotifyError(Exception):
    def __init__(self, status: int, message: str, url: str = ""):
        super().__init__(f"Spotify API errore {status}: {message} ({url})")
        self.status = status
        self.message = message
        self.url = url


class QuotaExceededError(SpotifyError):
    """Quota di Development Mode esaurita: inutile riprovare finché non scade il blocco."""

    def __init__(self, retry_after: int, url: str = ""):
        hours = retry_after / 3600
        super().__init__(
            429,
            f"QUOTA_EXCEEDED: Spotify chiede di riprovare tra circa {hours:.1f} ore",
            url,
        )
        self.retry_after = retry_after


def _error_reason(resp: requests.Response):
    """Legge il campo 'reason' del corpo di un errore, se presente."""
    try:
        body = resp.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return error.get("reason") if isinstance(error, dict) else None


class SpotifyClient:
    def __init__(self, tokens: TokenManager, timeout: int = 30, max_retries: int = 5):
        self.tokens = tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.request_count = 0

    def get(self, path_or_url: str, params: dict | None = None) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"{API_BASE}{path_or_url}"
        refreshed = False
        for attempt in range(1, self.max_retries + 1):
            headers = {"Authorization": f"Bearer {self.tokens.get_access_token()}"}
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                wait = min(2 ** attempt, 30)
                log.warning("Errore di rete (%s). Nuovo tentativo tra %ds", type(exc).__name__, wait)
                time.sleep(wait)
                continue

            self.request_count += 1
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401 and not refreshed:
                self.tokens.get_access_token(force_refresh=True)
                refreshed = True
                continue
            if resp.status_code == 429:
                try:
                    wait = int(float(resp.headers.get("Retry-After", "5"))) + 1
                except ValueError:
                    wait = 6
                if _error_reason(resp) == "QUOTA_EXCEEDED":
                    raise QuotaExceededError(wait, url)
                if wait > MAX_RETRY_AFTER:
                    raise SpotifyError(429, f"Rate limit: Spotify chiede di attendere {wait}s", url)
                log.warning("Rate limit raggiunto. Attendo %ds", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = min(2 ** attempt, 30)
                log.warning("Errore del server Spotify (%d). Nuovo tentativo tra %ds", resp.status_code, wait)
                time.sleep(wait)
                continue
            raise SpotifyError(resp.status_code, resp.text[:300], url)
        raise SpotifyError(0, "Troppi tentativi falliti", url)

    def paginate(self, path: str, params: dict | None = None, items_key: str | None = None):
        """Generatore che segue i link 'next'.

        items_key: chiave che contiene la pagina (es. 'artists' per /me/following).
        """
        data = self.get(path, params)
        while True:
            page = data[items_key] if items_key else data
            for item in page.get("items", []):
                yield item
            next_url = page.get("next")
            if not next_url:
                break
            data = self.get(next_url)