"""Controllo del profilo: python profile_check.py

Legge config/*.yaml e il database. Non chiama Spotify e non modifica niente.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from music_assistant.database import db  # noqa: E402
from music_assistant.profile.profile import ProfileError, load_profile  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    try:
        profile = load_profile()
    except ProfileError as exc:
        print(f"ERRORE nei file del profilo: {exc}")
        return 1

    summary = profile.summary()
    print("REGOLE LETTE DAI FILE")
    print(f"  artisti preferiti: {summary['favorite_artists']}")
    print(f"  generi preferiti: {summary['favorite_genres']}")
    print(f"  artisti in blacklist: {summary['blacklist_artists']}")
    print(f"  generi in blacklist: {summary['blacklist_genres']}")
    print(f"  parole vietate nei titoli: {summary['blacklist_keywords']}")
    print(f"  artisti con generi assegnati da te: {summary['artists_with_genres']}")

    conn = db.connect()
    rows = conn.execute(
        "SELECT artist_id, name, relevance FROM artists ORDER BY relevance DESC, name COLLATE NOCASE"
    ).fetchall()
    artists = [{"id": r["artist_id"], "name": r["name"], "relevance": r["relevance"]} for r in rows]
    if not artists:
        print("\nIl database non contiene ancora artisti. Esegui prima: python collect.py --max-artists 20")
        conn.close()
        return 0
    print(f"\nArtisti nel database: {len(artists)}")

    report = profile.match_report(artists)
    for title, key in (("PREFERITI", "favorites"), ("BLACKLIST ARTISTI", "blacklist")):
        matched, missing = report[key]
        if not matched and not missing:
            continue
        print(f"\n{title}")
        for raw, names in matched:
            note = "   <-- attenzione: piu' artisti con questo nome" if len(names) > 1 else ""
            print(f"  trovato: {raw} -> {', '.join(names)}{note}")
        for raw in missing:
            print(f"  NON TROVATO: {raw} (errore di battitura, oppure non e' tra gli artisti che segui o ascolti)")

    blacklisted = [a for a in artists if profile.is_blacklisted_artist(a["id"], a["name"])]
    if blacklisted:
        print(f"\nArtisti in blacklist presenti nel database: {len(blacklisted)} (non verranno controllati)")

    suggestions = [
        a for a in artists
        if not profile.is_favorite_artist(a["id"], a["name"]) and not profile.is_blacklisted_artist(a["id"], a["name"])
    ][:30]
    print("\nSUGGERIMENTI: i tuoi 30 artisti piu' importanti non ancora tra i preferiti")
    print("(copia le righe che vuoi sotto 'favorite_artists:' in config\\preferences.yaml)")
    for artist in suggestions:
        print(f"  - {json.dumps(artist['name'], ensure_ascii=False)}   # importanza {artist['relevance']}")

    windows = [r["window_key"] for r in conn.execute("SELECT DISTINCT window_key FROM releases ORDER BY window_key")]
    for window_key in windows:
        releases = db.releases_for_window(conn, window_key)
        discarded = []
        favorites = 0
        for release in releases:
            reason = profile.blacklist_reason(release)
            if reason:
                discarded.append((release, reason))
            elif profile.favorite_artists_in(release) or profile.has_favorite_genre(release):
                favorites += 1
        print(f"\nUSCITE GIA' SALVATE, mese {window_key}: {len(releases)}")
        print(f"  scartate dalla blacklist: {len(discarded)}")
        print(f"  di artisti o generi preferiti: {favorites}")
        for release, reason in discarded[:8]:
            names = ", ".join(a["name"] for a in release["artists"])
            print(f"    scartata ({reason}): {names} - {release['title']}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())