"""Raccolta dati da Spotify: universo di artisti e uscite del periodo."""
import logging
import time
from datetime import date
from itertools import islice

from ..dates import release_in_window
from .client import QuotaExceededError, SpotifyClient, SpotifyError

log = logging.getLogger(__name__)

SEARCH_PAGE_SIZE = 10   # massimo consentito oggi da Spotify per /search
MAX_SEARCH_PAGES = 3    # tetto di pagine per artista: oltre, segnaliamo il possibile taglio

# Peso di ogni fonte nel calcolo dell'importanza di un artista
SOURCE_WEIGHTS = {
    "top_short_term": 5,
    "top_medium_term": 4,
    "top_long_term": 3,
    "recent": 3,
    "saved_album": 3,
    "saved_track": 2,
    "followed": 2,
}


def _add_artist(universe: dict, artist: dict, source: str) -> None:
    artist_id = artist.get("id")
    if not artist_id:
        return
    entry = universe.setdefault(
        artist_id, {"id": artist_id, "name": artist.get("name", ""), "genres": None, "sources": []}
    )
    if artist.get("genres") is not None:  # Spotify oggi non li restituisce, ma se tornassero li usiamo
        entry["genres"] = artist["genres"]
    if source not in entry["sources"]:
        entry["sources"].append(source)


def build_artist_universe(client: SpotifyClient, cfg: dict) -> dict:
    """Insieme degli artisti collegati ai tuoi gusti."""
    universe: dict = {}

    followed = list(client.paginate("/me/following", {"type": "artist", "limit": 50}, items_key="artists"))
    for artist in followed:
        _add_artist(universe, artist, "followed")
    log.info("Artisti seguiti: %d", len(followed))

    for time_range in ("short_term", "medium_term", "long_term"):
        top = list(client.paginate("/me/top/artists", {"time_range": time_range, "limit": 50}))
        for artist in top:
            _add_artist(universe, artist, f"top_{time_range}")
    log.info("Dopo i top artisti: %d artisti", len(universe))

    albums = list(islice(client.paginate("/me/albums", {"limit": 50}), int(cfg.get("saved_albums_max", 200))))
    for item in albums:
        for artist in item.get("album", {}).get("artists", []):
            _add_artist(universe, artist, "saved_album")
    log.info("Album salvati letti: %d", len(albums))

    recent = client.get("/me/player/recently-played", {"limit": 50}).get("items", [])
    for item in recent:
        for artist in item.get("track", {}).get("artists", []):
            _add_artist(universe, artist, "recent")
    log.info("Brani recenti letti: %d", len(recent))

    tracks = list(islice(client.paginate("/me/tracks", {"limit": 50}), int(cfg.get("saved_tracks_max", 500))))
    for item in tracks:
        for artist in item.get("track", {}).get("artists", []):
            _add_artist(universe, artist, "saved_track")
    log.info("Brani salvati letti: %d", len(tracks))

    log.info("Artisti totali nell'universo: %d", len(universe))
    return universe


def rank_universe(universe: dict) -> list:
    """Elenco degli artisti con il campo 'relevance', dal più importante al meno."""
    ranked = []
    for entry in universe.values():
        relevance = sum(SOURCE_WEIGHTS.get(source, 1) for source in entry["sources"])
        ranked.append({**entry, "relevance": relevance})
    ranked.sort(key=lambda a: (-a["relevance"], a["name"].lower()))
    return ranked


def _search_artist_releases(client: SpotifyClient, artist: dict, year: int):
    """Cerca le uscite dell'anno di un artista con /search.

    La ricerca è approssimativa sul nome (restituisce anche omonimi), quindi
    teniamo solo le uscite in cui compare l'ID dell'artista cercato.
    Restituisce (uscite_dell_artista, risultati_visti, tetto_raggiunto).
    """
    name = (artist.get("name") or "").replace('"', " ").strip()
    query = f'artist:"{name}" year:{year}'
    seen: list = []
    capped = False
    pages = 1
    data = client.get("/search", {"q": query, "type": "album", "limit": SEARCH_PAGE_SIZE})
    while True:
        page = data.get("albums", {})
        seen.extend(page.get("items", []))
        if not page.get("next"):
            break
        if pages >= MAX_SEARCH_PAGES:
            capped = True
            break
        data = client.get(page["next"])
        pages += 1
    mine = [
        album for album in seen
        if any(credited.get("id") == artist["id"] for credited in album.get("artists", []))
    ]
    return mine, len(seen), capped


def collect_releases(client: SpotifyClient, artists: list, start: date, end: date, cfg: dict,
                     on_artist_done=None):
    """Cerca le uscite del periodo per l'elenco di artisti ricevuto.

    on_artist_done(artist, releases, capped) viene chiamata dopo ogni artista
    completato, così il chiamante può salvare subito i progressi.
    Se Spotify blocca per quota o rate limit, si ferma subito e restituisce
    quanto raccolto finora (stats['stopped_early'] = True).
    """
    delay = float(cfg.get("request_delay_seconds", 0.15))
    year = start.year  # la finestra è sempre un mese, quindi dentro un solo anno

    found: dict = {}
    stats = {
        "artists_requested": len(artists),
        "artists_completed": 0,
        "artist_errors": 0,
        "artists_capped": 0,
        "artists_noise_only": 0,
        "skipped_year_precision": 0,
        "stopped_early": False,
        "quota_exceeded": False,
        "retry_after_hours": None,
    }

    for index, artist in enumerate(artists, start=1):
        try:
            albums, seen, capped = _search_artist_releases(client, artist, year)
        except QuotaExceededError as exc:
            stats["quota_exceeded"] = True
            stats["stopped_early"] = True
            stats["retry_after_hours"] = round(exc.retry_after / 3600, 1)
            log.error(
                "Quota Spotify esaurita dopo %d artisti completati. Riprovare tra circa %.1f ore.",
                stats["artists_completed"],
                exc.retry_after / 3600,
            )
            break
        except SpotifyError as exc:
            if exc.status in (401, 403):
                raise
            if exc.status == 429:
                stats["stopped_early"] = True
                log.error("Rate limit Spotify: mi fermo dopo %d artisti completati (%s)", stats["artists_completed"], exc)
                break
            stats["artist_errors"] += 1
            log.warning("Artista %s saltato: %s", artist["name"], exc)
            continue

        stats["artists_completed"] += 1
        if seen and not albums:
            stats["artists_noise_only"] += 1  # risultati trovati, ma solo di omonimi
        if capped:
            stats["artists_capped"] += 1
            log.warning("Artista %s: raggiunto il tetto di %d pagine di risultati", artist["name"], MAX_SEARCH_PAGES)

        artist_releases = []
        for album in albums:
            precision = album.get("release_date_precision", "day")
            if not release_in_window(album.get("release_date", ""), precision, start, end):
                if precision == "year":
                    stats["skipped_year_precision"] += 1
                continue
            release = {
                "spotify_id": album["id"],
                "title": album.get("name", ""),
                "album_type": album.get("album_type", ""),
                "release_date": album.get("release_date", ""),
                "release_date_precision": precision,
                "total_tracks": album.get("total_tracks"),
                "artists": [{"id": a.get("id"), "name": a.get("name", "")} for a in album.get("artists", [])],
                "spotify_url": album.get("external_urls", {}).get("spotify", ""),
                "genres": [],  # Spotify oggi non fornisce i generi
            }
            artist_releases.append(release)
            found.setdefault(release["spotify_id"], release)

        if on_artist_done:
            on_artist_done(artist, artist_releases, capped)

        if index % 50 == 0:
            log.info("Controllati %d/%d artisti, release trovate finora: %d", index, len(artists), len(found))
        time.sleep(delay)

    stats["requests"] = client.request_count
    releases = sorted(found.values(), key=lambda r: r["release_date"], reverse=True)
    stats["releases"] = len(releases)
    log.info(
        "Ricerca: artisti con soli omonimi trovati: %d, con tetto di pagine raggiunto: %d",
        stats["artists_noise_only"], stats["artists_capped"],
    )
    return releases, stats