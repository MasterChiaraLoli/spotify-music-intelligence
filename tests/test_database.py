"""Test rapido del database, in memoria: python tests\\test_database.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from music_assistant.database import db  # noqa: E402

conn = db.connect(":memory:")

seen = db.upsert_artists(conn, [
    {"id": "a1", "name": "Artista Uno", "sources": ["followed"], "relevance": 2},
    {"id": "a2", "name": "Artista Due", "sources": ["top_short_term"], "relevance": 5},
])

# gli artisti più rilevanti vengono per primi
pending = db.pending_artists(conn, "2026-09", seen)
assert [a["id"] for a in pending] == ["a2", "a1"], pending

# dopo il controllo di a2, resta solo a1
db.mark_artist_scanned(conn, "a2", "2026-09", capped=True)
assert [a["id"] for a in db.pending_artists(conn, "2026-09", seen)] == ["a1"]
# per un altro mese bisogna rifare tutti
assert [a["id"] for a in db.pending_artists(conn, "2026-10", seen)] == ["a2", "a1"]

release = {
    "spotify_id": "r1", "title": "Titolo", "album_type": "single",
    "release_date": "2026-09-05", "release_date_precision": "day", "total_tracks": 1,
    "artists": [{"id": "a1", "name": "Artista Uno"}], "genres": [],
    "spotify_url": "https://open.spotify.com/album/r1",
}
assert db.save_releases(conn, [release], "2026-09") == 1   # nuova
assert db.save_releases(conn, [release], "2026-09") == 0   # già presente: nessun doppione
rows = db.releases_for_window(conn, "2026-09")
assert len(rows) == 1 and rows[0]["title"] == "Titolo" and rows[0]["artist_name"] == "Artista Uno"

assert db.window_progress(conn, "2026-09", seen) == {"total": 2, "done": 1, "capped": 1}

run_id = db.start_run(conn, "2026-09")
db.finish_run(conn, run_id, "ok", artists_scanned=1, releases_found=1, requests=3)

print("OK: database funzionante")