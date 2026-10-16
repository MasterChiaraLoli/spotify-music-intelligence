"""Raccolta delle uscite del mese precedente, con storico nel database.

Uso:
    python collect.py                    # mese precedente; riprende dagli artisti non ancora controllati
    python collect.py --month 2026-09    # un mese specifico
    python collect.py --max-artists 20   # al massimo 20 artisti in questa corsa (test)
"""
import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from music_assistant.database import db  # noqa: E402
from music_assistant.dates import month_window, previous_month_window  # noqa: E402
from music_assistant.settings import load_settings, setup_logging  # noqa: E402
from music_assistant.spotify.auth import TokenManager  # noqa: E402
from music_assistant.spotify.client import SpotifyClient  # noqa: E402
from music_assistant.spotify.collector import (  # noqa: E402
    build_artist_universe,
    collect_releases,
    rank_universe,
)

log = logging.getLogger("collect")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", help="Mese da analizzare, formato AAAA-MM (default: mese precedente)")
    parser.add_argument("--max-artists", type=int, help="Numero massimo di artisti da controllare in questa corsa")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings.log_level)
    log.info("START")

    if args.month:
        year, month = (int(part) for part in args.month.split("-"))
        start, end = month_window(year, month)
    else:
        start, end = previous_month_window(settings.timezone)
    window_key = f"{start.year}-{start.month:02d}"
    log.info("Finestra temporale: %s -> %s", start, end)

    conn = db.connect()
    run_id = db.start_run(conn, window_key)
    client = None
    status, note = "error", None
    completed = 0
    counters = {"new": 0}
    exit_code = 0

    try:
        tokens = TokenManager(settings.client_id, settings.client_secret, settings.redirect_uri, settings.token_path)
        client = SpotifyClient(tokens)
        client.get("/me")
        log.info("Spotify authentication OK")

        cfg = dict(settings.app.get("collection", {}))
        universe = build_artist_universe(client, cfg)
        seen_since = db.upsert_artists(conn, rank_universe(universe))

        per_run = args.max_artists or int(cfg.get("max_artists_to_scan", 400))
        pending = db.pending_artists(conn, window_key, seen_since, limit=per_run)
        before = db.window_progress(conn, window_key, seen_since)
        log.info(
            "Mese %s: artisti totali %d, gia' controllati %d, in questa corsa ne controllo %d",
            window_key, before["total"], before["done"], len(pending),
        )

        def on_artist_done(artist, releases, capped):
            counters["new"] += db.save_releases(conn, releases, window_key)
            db.mark_artist_scanned(conn, artist["id"], window_key, capped)

        if pending:
            _, stats = collect_releases(client, pending, start, end, cfg, on_artist_done=on_artist_done)
            completed = stats["artists_completed"]
            log.info("Artisti controllati in questa corsa: %d (errori: %d)", completed, stats["artist_errors"])
            if stats["quota_exceeded"]:
                log.error(
                    "RACCOLTA INCOMPLETA: quota Spotify esaurita. Rilancia tra circa %s ore: riprende da dove si e' fermata.",
                    stats["retry_after_hours"],
                )
                exit_code = 2
            elif stats["stopped_early"]:
                log.error("RACCOLTA INCOMPLETA: fermata per un limite di Spotify. Rilancia piu' tardi.")
                exit_code = 2
        else:
            log.info("Tutti gli artisti sono gia' stati controllati per questo mese.")

        after = db.window_progress(conn, window_key, seen_since)
        rows = db.releases_for_window(conn, window_key)
        types = Counter(r["release_type"] for r in rows)
        log.info(
            "Progresso del mese: %d/%d artisti controllati (con controllo parziale: %d)",
            after["done"], after["total"], after["capped"],
        )
        log.info(
            "Uscite nuove salvate: %d. Totale uscite del mese nello storico: %d (%s)",
            counters["new"], len(rows), ", ".join(f"{k}: {v}" for k, v in sorted(types.items())) or "nessuna",
        )
        log.info("Richieste a Spotify in questa corsa: %d", client.request_count)

        complete = after["done"] >= after["total"]
        if not complete and exit_code == 0:
            log.info("Mancano %d artisti: rilancia il comando per continuare.", after["total"] - after["done"])
        status = "ok" if complete else "partial"

        print("\nPrime uscite del mese nello storico:")
        for row in rows[:15]:
            names = ", ".join(a["name"] for a in row["artists"])
            print(f"  {row['release_date']}  {row['release_type']:<11}  {names} - {row['title']}")
        log.info("END" if complete else "END (incompleta)")
        return exit_code
    except Exception as exc:
        note = str(exc)[:300]
        raise
    finally:
        db.finish_run(
            conn, run_id, status, artists_scanned=completed, releases_found=counters["new"],
            requests=client.request_count if client else 0, note=note,
        )
        conn.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("collect").error("%s", exc)
        sys.exit(1)