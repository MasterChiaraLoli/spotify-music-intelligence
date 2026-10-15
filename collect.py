"""Test STEP 5: raccolta delle uscite del mese precedente.

Uso:
    python collect.py                    # mese precedente a oggi
    python collect.py --month 2026-09    # un mese specifico
    python collect.py --max-artists 20   # controlla solo i primi 20 artisti (test sulla quota)
"""
import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from music_assistant.dates import month_window, previous_month_window  # noqa: E402
from music_assistant.settings import load_settings, setup_logging  # noqa: E402
from music_assistant.spotify.auth import TokenManager  # noqa: E402
from music_assistant.spotify.client import SpotifyClient  # noqa: E402
from music_assistant.spotify.collector import build_artist_universe, collect_releases  # noqa: E402

log = logging.getLogger("collect")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", help="Mese da analizzare, formato AAAA-MM (default: mese precedente)")
    parser.add_argument("--max-artists", type=int, help="Numero massimo di artisti da controllare (per i test)")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings.log_level)
    log.info("START")

    if args.month:
        year, month = (int(part) for part in args.month.split("-"))
        start, end = month_window(year, month)
    else:
        start, end = previous_month_window(settings.timezone)
    log.info("Finestra temporale: %s -> %s", start, end)

    tokens = TokenManager(settings.client_id, settings.client_secret, settings.redirect_uri, settings.token_path)
    client = SpotifyClient(tokens)
    me = client.get("/me")
    log.info("Spotify authentication OK (account: %s)", me.get("display_name") or me.get("id"))

    cfg = dict(settings.app.get("collection", {}))
    if args.max_artists:
        cfg["max_artists_to_scan"] = args.max_artists
        log.info("Test: controllo al massimo %d artisti", args.max_artists)

    universe = build_artist_universe(client, cfg)
    releases, stats = collect_releases(client, universe, start, end, cfg)

    types = Counter(r["album_type"] for r in releases)
    log.info("Release trovate: %d (%s)", len(releases), ", ".join(f"{k}: {v}" for k, v in sorted(types.items())) or "nessuna")
    log.info(
        "Artisti completati: %d su %d da controllare (universo: %d), senza generi noti: %d",
        stats["artists_completed"], stats["artists_scanned"], stats["artists_in_universe"], stats["artists_without_genres"],
    )
    log.info("Errori su singoli artisti: %d, artisti con tetto raggiunto: %d", stats["artist_errors"], stats["artists_capped"])
    log.info("Release scartate per data solo-anno: %d, richieste totali: %d", stats["skipped_year_precision"], stats["requests"])

    out_path = ROOT / "data" / "last_collection.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(
        json.dumps(
            {"window": [str(start), str(end)], "partial": stats["stopped_early"], "stats": stats, "releases": releases},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info("Dati salvati in %s", out_path.relative_to(ROOT))

    print("\nPrime release trovate:")
    for release in releases[:15]:
        names = ", ".join(a["name"] for a in release["artists"])
        genres = ", ".join(release["genres"][:3]) if release["genres"] else "generi n/d"
        print(f"  {release['release_date']}  {release['album_type']:<8}  {names} - {release['title']}  [{genres}]")

    if stats["stopped_early"]:
        if stats["quota_exceeded"]:
            log.error(
                "RACCOLTA INCOMPLETA: quota Spotify esaurita dopo %d artisti. Riprovare tra circa %s ore.",
                stats["artists_completed"], stats["retry_after_hours"],
            )
        else:
            log.error("RACCOLTA INCOMPLETA: fermata dopo %d artisti per un limite di Spotify.", stats["artists_completed"])
        log.info("END (incompleta)")
        return 2

    log.info("END")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("collect").error("%s", exc)
        sys.exit(1)