"""Login Spotify, da eseguire una sola volta: python authorize.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from music_assistant.settings import load_settings, setup_logging  # noqa: E402
from music_assistant.spotify.auth import TokenManager  # noqa: E402
from music_assistant.spotify.client import SpotifyClient  # noqa: E402


def main() -> int:
    settings = load_settings()
    setup_logging(settings.log_level)
    tokens = TokenManager(settings.client_id, settings.client_secret, settings.redirect_uri, settings.token_path)
    tokens.authorize()
    me = SpotifyClient(tokens).get("/me")
    print(f"\nAutorizzazione completata. Account collegato: {me.get('display_name') or me.get('id')}")
    print("Il permesso è salvato in .spotify_token.json (file privato, non finisce su GitHub).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"\nERRORE: {exc}")
        sys.exit(1)