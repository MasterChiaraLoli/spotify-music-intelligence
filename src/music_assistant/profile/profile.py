"""Profilo musicale: preferiti, blacklist e generi assegnati a mano."""
import re
import unicodedata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT / "config"

_ID_RE = re.compile(r"^[0-9A-Za-z]{22}$")
_URL_RE = re.compile(r"artist[/:]([0-9A-Za-z]{22})")


class ProfileError(ValueError):
    """Errore di formato nei file di configurazione del profilo."""


def normalize(text) -> str:
    """Minuscolo, senza accenti né punteggiatura, spazi singoli: per confrontare i nomi."""
    text = unicodedata.normalize("NFKD", str(text)).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _key(text) -> str:
    original = str(text)
    return normalize(original) or original.casefold().strip()


def _extract_id(value):
    value = str(value).strip()
    match = _URL_RE.search(value)
    if match:
        return match.group(1)
    if _ID_RE.match(value):
        return value
    return None


class Entry:
    """Una voce dell'elenco (artista preferito o in blacklist)."""

    def __init__(self, raw: str):
        self.raw = raw
        self.artist_id = _extract_id(raw)
        self.key = _key(raw)


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path.name}: errore di formato YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{path.name}: formato non valido")
    return data


def _as_list(value, filename: str, field: str) -> list:
    if value is None:
        return []
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        raise ProfileError(f"{filename}, campo '{field}': deve essere un elenco")
    result = []
    for item in value:
        if item is None:
            continue
        if isinstance(item, bool):
            raise ProfileError(
                f"{filename}, campo '{field}': trovato il valore {item} senza virgolette. "
                'Scrivi i nomi tra virgolette, per esempio "Yes".'
            )
        if isinstance(item, (dict, list)):
            raise ProfileError(
                f"{filename}, campo '{field}': voce non valida. "
                "Se il nome contiene i due punti, scrivilo tra virgolette."
            )
        text = str(item).strip()
        if text:
            result.append(text)
    return result


class Profile:
    def __init__(self, favorites, blacklist, favorite_genres, blacklist_genres,
                 keywords, genres_by_name, genres_by_id):
        self.favorites = favorites                # elenco di Entry
        self.blacklist = blacklist                # elenco di Entry
        self.favorite_genres = favorite_genres    # insieme di generi normalizzati
        self.blacklist_genres = blacklist_genres  # insieme di generi normalizzati
        self.keywords = keywords                  # elenco di (testo originale, chiave normalizzata)
        self.genres_by_name = genres_by_name      # nome normalizzato -> generi
        self.genres_by_id = genres_by_id          # ID artista -> generi
        self._fav_names = {e.key for e in favorites}
        self._fav_ids = {e.artist_id for e in favorites if e.artist_id}
        self._black_names = {e.key for e in blacklist}
        self._black_ids = {e.artist_id for e in blacklist if e.artist_id}

    # ---------------------------------------------------------- artisti
    def is_favorite_artist(self, artist_id, name) -> bool:
        return artist_id in self._fav_ids or _key(name) in self._fav_names

    def is_blacklisted_artist(self, artist_id, name) -> bool:
        return artist_id in self._black_ids or _key(name) in self._black_names

    # ------------------------------------------------------------ generi
    def genres_for_artist(self, artist_id, name) -> list:
        genres = list(self.genres_by_id.get(artist_id, []))
        for genre in self.genres_by_name.get(_key(name), []):
            if genre not in genres:
                genres.append(genre)
        return genres

    def genres_for_release(self, release: dict) -> list:
        genres: list = []
        for credited in release.get("artists", []):
            for genre in self.genres_for_artist(credited.get("id"), credited.get("name", "")):
                if genre not in genres:
                    genres.append(genre)
        for genre in release.get("genres") or []:  # i generi di Spotify, se tornassero
            key = _key(genre)
            if key not in genres:
                genres.append(key)
        return genres

    # ------------------------------------------------------------ uscite
    def favorite_artists_in(self, release: dict) -> list:
        return [
            c.get("name", "") for c in release.get("artists", [])
            if self.is_favorite_artist(c.get("id"), c.get("name", ""))
        ]

    def has_favorite_genre(self, release: dict) -> bool:
        return any(g in self.favorite_genres for g in self.genres_for_release(release))

    def blacklist_reason(self, release: dict):
        """None se l'uscita è ammessa, altrimenti il motivo dello scarto."""
        for credited in release.get("artists", []):
            if self.is_blacklisted_artist(credited.get("id"), credited.get("name", "")):
                return f"artista in blacklist: {credited.get('name', '')}"
        for genre in self.genres_for_release(release):
            if genre in self.blacklist_genres:
                return f"genere in blacklist: {genre}"
        title = f" {_key(release.get('title', ''))} "
        for raw, key in self.keywords:
            if key and f" {key} " in title:
                return f"parola nel titolo: {raw}"
        return None

    # ------------------------------------------------------ verifica
    def summary(self) -> dict:
        return {
            "favorite_artists": len(self.favorites),
            "favorite_genres": len(self.favorite_genres),
            "blacklist_artists": len(self.blacklist),
            "blacklist_genres": len(self.blacklist_genres),
            "blacklist_keywords": len(self.keywords),
            "artists_with_genres": len(self.genres_by_name) + len(self.genres_by_id),
        }

    def match_report(self, artists: list) -> dict:
        """artists: elenco di dict con 'id' e 'name'.

        Per preferiti e blacklist dice quali voci hanno trovato un artista e quali no.
        """
        by_id = {a["id"]: a for a in artists}
        by_key: dict = {}
        for artist in artists:
            by_key.setdefault(_key(artist["name"]), []).append(artist)

        def resolve(entries):
            matched, missing = [], []
            for entry in entries:
                hits = []
                if entry.artist_id and entry.artist_id in by_id:
                    hits.append(by_id[entry.artist_id])
                for artist in by_key.get(entry.key, []):
                    if artist not in hits:
                        hits.append(artist)
                if hits:
                    matched.append((entry.raw, [h["name"] for h in hits]))
                else:
                    missing.append(entry.raw)
            return matched, missing

        return {"favorites": resolve(self.favorites), "blacklist": resolve(self.blacklist)}


def load_profile(config_dir=None) -> Profile:
    folder = Path(config_dir) if config_dir else CONFIG_DIR

    prefs = _read_yaml(folder / "preferences.yaml")
    black = _read_yaml(folder / "blacklist.yaml")
    genres_file = _read_yaml(folder / "artist_genres.yaml")

    favorites = [Entry(x) for x in _as_list(prefs.get("favorite_artists"), "preferences.yaml", "favorite_artists")]
    favorite_genres = {_key(x) for x in _as_list(prefs.get("favorite_genres"), "preferences.yaml", "favorite_genres")}

    blacklist = [Entry(x) for x in _as_list(black.get("blacklist_artists"), "blacklist.yaml", "blacklist_artists")]
    blacklist_genres = {_key(x) for x in _as_list(black.get("blacklist_genres"), "blacklist.yaml", "blacklist_genres")}
    keywords = [
        (x, _key(x))
        for x in _as_list(black.get("blacklist_title_keywords"), "blacklist.yaml", "blacklist_title_keywords")
    ]

    genres_by_name: dict = {}
    genres_by_id: dict = {}
    raw_map = genres_file.get("artist_genres") or {}
    if not isinstance(raw_map, dict):
        raise ProfileError("artist_genres.yaml, campo 'artist_genres': deve essere una mappa nome: [generi]")
    for artist_key, value in raw_map.items():
        if isinstance(artist_key, bool):
            raise ProfileError(
                f"artist_genres.yaml: trovato il nome {artist_key} senza virgolette. "
                'Scrivi i nomi tra virgolette, per esempio "Yes".'
            )
        genres = [_key(g) for g in _as_list(value, "artist_genres.yaml", str(artist_key))]
        artist_id = _extract_id(artist_key)
        if artist_id:
            genres_by_id[artist_id] = genres
        else:
            genres_by_name[_key(artist_key)] = genres

    return Profile(favorites, blacklist, favorite_genres, blacklist_genres, keywords, genres_by_name, genres_by_id)