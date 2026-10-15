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
    """Insieme degli artisti collegati ai tuoi gusti, in ordine di priorità."""
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


def collect_releases(client: SpotifyClient, universe: dict, start: date, end: date, cfg: dict):
    """Cerca le uscite del periodo per gli artisti dell'universo.

    Se Spotify blocca per quota o rate limit, si ferma subito e restituisce
    quanto raccolto finora (stats['stopped_early'] = True).
    """
    max_artists = int(cfg.get("max_artists_to_scan", 400))
    delay = float(cfg.get("request_delay_seconds", 0.15))
    year = start.year  # la finestra è sempre un mese, quindi dentro un solo anno

    artists = list(universe.values())[:max_artists]
    if len(universe) > max_artists:
        log.warning("Artisti nell'universo: %d. Ne controllo solo %d (max_artists_to_scan)", len(universe), max_artists)

    found: dict = {}
    stats = {
        "artists_in_universe": len(universe),
        "artists_scanned": len(artists),
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

        for album in albums:
            precision = album.get("release_date_precision", "day")
            if not release_in_window(album.get("release_date", ""), precision, start, end):
                if precision == "year":
                    stats["skipped_year_precision"] += 1
                continue
            existing = found.get(album["id"])
            if existing is None:
                found[album["id"]] = {
                    "spotify_id": album["id"],
                    "title": album.get("name", ""),
                    "album_type": album.get("album_type", ""),
                    "release_date": album.get("release_date", ""),
                    "release_date_precision": precision,
                    "total_tracks": album.get("total_tracks"),
                    "artists": [
                        {"id": a.get("id"), "name": a.get("name", "")} for a in album.get("artists", [])
                    ],
                    "spotify_url": album.get("external_urls", {}).get("spotify", ""),
                    "scanned_artist_ids": [artist["id"]],
                }
            elif artist["id"] not in existing["scanned_artist_ids"]:
                existing["scanned_artist_ids"].append(artist["id"])

        if index % 50 == 0:
            log.info("Controllati %d/%d artisti, release trovate finora: %d", index, len(artists), len(found))
        time.sleep(delay)

    # generi: unione dei generi degli artisti accreditati che conosciamo
    for release in found.values():
        genres: list = []
        known = False
        for credited in release["artists"]:
            info = universe.get(credited["id"])
            if info and info["genres"] is not None:
                known = True
                for genre in info["genres"]:
                    if genre not in genres:
                        genres.append(genre)
        release["genres"] = genres
        release["genres_known"] = known

    stats["artists_without_genres"] = sum(1 for a in artists if a["genres"] is None)
    stats["requests"] = client.request_count
    releases = sorted(found.values(), key=lambda r: r["release_date"], reverse=True)
    stats["releases"] = len(releases)
    log.info(
        "Ricerca: artisti con soli omonimi trovati: %d, con tetto di pagine raggiunto: %d",
        stats["artists_noise_only"], stats["artists_capped"],
    )
    return releases, stats