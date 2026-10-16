"""Database SQLite dello storico: artisti, uscite, valutazioni, esecuzioni."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = ROOT / "data" / "music.sqlite"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    artist_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    sources         TEXT NOT NULL DEFAULT '[]',
    relevance       INTEGER NOT NULL DEFAULT 0,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    scanned_window  TEXT,
    scan_capped     INTEGER NOT NULL DEFAULT 0,
    scanned_at      TEXT
);

CREATE TABLE IF NOT EXISTS releases (
    spotify_id              TEXT PRIMARY KEY,
    title                   TEXT NOT NULL,
    release_type            TEXT NOT NULL,
    release_date            TEXT NOT NULL,
    release_date_precision  TEXT NOT NULL,
    total_tracks            INTEGER,
    artists_json            TEXT NOT NULL,
    artist_id               TEXT,
    artist_name             TEXT,
    genres_json             TEXT NOT NULL DEFAULT '[]',
    spotify_url             TEXT,
    window_key              TEXT NOT NULL,
    first_seen              TEXT NOT NULL,
    score                   REAL,
    classification          TEXT,
    playlist                TEXT
);
CREATE INDEX IF NOT EXISTS idx_releases_window ON releases(window_key);
CREATE INDEX IF NOT EXISTS idx_releases_artist ON releases(artist_id);

CREATE TABLE IF NOT EXISTS ratings (
    spotify_id  TEXT PRIMARY KEY,
    rating      TEXT NOT NULL,
    rated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    window_key       TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL,
    artists_scanned  INTEGER NOT NULL DEFAULT 0,
    releases_found   INTEGER NOT NULL DEFAULT 0,
    requests         INTEGER NOT NULL DEFAULT 0,
    note             TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None) -> sqlite3.Connection:
    """Apre (e se serve crea) il database. path=':memory:' per i test."""
    if path == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        db_path = Path(path) if path else DEFAULT_DB_PATH
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


# ---------------------------------------------------------------- artisti
def upsert_artists(conn: sqlite3.Connection, artists: list) -> str:
    """Inserisce o aggiorna gli artisti. Restituisce il momento usato come 'visto'."""
    now = _now()
    conn.executemany(
        """
        INSERT INTO artists (artist_id, name, sources, relevance, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(artist_id) DO UPDATE SET
            name = excluded.name,
            sources = excluded.sources,
            relevance = excluded.relevance,
            last_seen = excluded.last_seen
        """,
        [(a["id"], a["name"], json.dumps(a["sources"]), a["relevance"], now, now) for a in artists],
    )
    conn.commit()
    return now


def pending_artists(conn: sqlite3.Connection, window_key: str, seen_since: str, limit=None) -> list:
    """Artisti ancora da controllare per il mese indicato, dal più rilevante."""
    sql = """
        SELECT artist_id, name, sources, relevance FROM artists
        WHERE last_seen >= ? AND scanned_window IS NOT ?
        ORDER BY relevance DESC, name COLLATE NOCASE ASC
    """
    params: list = [seen_since, window_key]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": row["artist_id"],
            "name": row["name"],
            "sources": json.loads(row["sources"]),
            "relevance": row["relevance"],
            "genres": None,
        }
        for row in rows
    ]


def mark_artist_scanned(conn: sqlite3.Connection, artist_id: str, window_key: str, capped: bool) -> None:
    conn.execute(
        "UPDATE artists SET scanned_window = ?, scan_capped = ?, scanned_at = ? WHERE artist_id = ?",
        (window_key, 1 if capped else 0, _now(), artist_id),
    )
    conn.commit()


def window_progress(conn: sqlite3.Connection, window_key: str, seen_since: str) -> dict:
    """Quanti artisti sono stati controllati per il mese indicato."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN scanned_window = ? THEN 1 ELSE 0 END), 0) AS done,
               COALESCE(SUM(CASE WHEN scanned_window = ? AND scan_capped = 1 THEN 1 ELSE 0 END), 0) AS capped
        FROM artists WHERE last_seen >= ?
        """,
        (window_key, window_key, seen_since),
    ).fetchone()
    return {"total": row["total"], "done": row["done"], "capped": row["capped"]}


# ------------------------------------------------------------------ uscite
def count_releases(conn: sqlite3.Connection, window_key=None) -> int:
    if window_key:
        row = conn.execute("SELECT COUNT(*) AS n FROM releases WHERE window_key = ?", (window_key,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM releases").fetchone()
    return row["n"]


def save_releases(conn: sqlite3.Connection, releases: list, window_key: str) -> int:
    """Salva le uscite senza duplicati. Restituisce quante sono nuove.

    Per quelle già presenti aggiorna i dati di Spotify ma conserva first_seen,
    punteggio, classificazione e playlist.
    """
    if not releases:
        return 0
    before = count_releases(conn)
    now = _now()
    rows = []
    for release in releases:
        first = release["artists"][0] if release["artists"] else {"id": None, "name": ""}
        rows.append(
            (
                release["spotify_id"],
                release["title"],
                release["album_type"],
                release["release_date"],
                release["release_date_precision"],
                release.get("total_tracks"),
                json.dumps(release["artists"], ensure_ascii=False),
                first.get("id"),
                first.get("name"),
                json.dumps(release.get("genres", []), ensure_ascii=False),
                release.get("spotify_url", ""),
                window_key,
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO releases (spotify_id, title, release_type, release_date, release_date_precision,
                              total_tracks, artists_json, artist_id, artist_name, genres_json,
                              spotify_url, window_key, first_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(spotify_id) DO UPDATE SET
            title = excluded.title,
            release_type = excluded.release_type,
            release_date = excluded.release_date,
            release_date_precision = excluded.release_date_precision,
            total_tracks = excluded.total_tracks,
            artists_json = excluded.artists_json,
            artist_id = excluded.artist_id,
            artist_name = excluded.artist_name,
            genres_json = excluded.genres_json,
            spotify_url = excluded.spotify_url
        """,
        rows,
    )
    conn.commit()
    return count_releases(conn) - before


def releases_for_window(conn: sqlite3.Connection, window_key: str, limit=None) -> list:
    sql = "SELECT * FROM releases WHERE window_key = ? ORDER BY release_date DESC, title COLLATE NOCASE ASC"
    params: list = [window_key]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    result = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        item["artists"] = json.loads(item.pop("artists_json"))
        item["genres"] = json.loads(item.pop("genres_json"))
        result.append(item)
    return result


# ------------------------------------------------------------- esecuzioni
def start_run(conn: sqlite3.Connection, window_key: str) -> int:
    cursor = conn.execute(
        "INSERT INTO runs (window_key, started_at, status) VALUES (?, ?, 'running')",
        (window_key, _now()),
    )
    conn.commit()
    return cursor.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, artists_scanned: int,
               releases_found: int, requests: int, note=None) -> None:
    conn.execute(
        """
        UPDATE runs SET finished_at = ?, status = ?, artists_scanned = ?,
                        releases_found = ?, requests = ?, note = ?
        WHERE run_id = ?
        """,
        (_now(), status, artists_scanned, releases_found, requests, note, run_id),
    )
    conn.commit()