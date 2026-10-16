"""Test del profilo, con file temporanei: python tests\\test_profile.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from music_assistant.profile.profile import ProfileError, load_profile, normalize  # noqa: E402


def write(folder, name, text):
    (Path(folder) / name).write_text(text, encoding="utf-8")


assert normalize("  Beyoncé ") == "beyonce"
assert normalize("Mrs. GREEN APPLE") == "mrs green apple"

with tempfile.TemporaryDirectory() as tmp:
    write(tmp, "preferences.yaml", '''favorite_artists:
  - "Beyoncé"
  - "4x1nvY2FN8jxqAFA0DA02H"
favorite_genres:
  - "Pop"
''')
    write(tmp, "blacklist.yaml", '''blacklist_artists:
  - "Artista Escluso"
blacklist_genres:
  - "Hardstyle"
blacklist_title_keywords:
  - "8D Audio"
  - "cover"
''')
    write(tmp, "artist_genres.yaml", '''artist_genres:
  "Artista Pop": ["pop"]
  "Artista Duro": ["hardstyle"]
''')
    p = load_profile(tmp)

    # artisti preferiti: per nome (senza accenti/maiuscole) e per ID
    assert p.is_favorite_artist("x", "BEYONCE")
    assert p.is_favorite_artist("4x1nvY2FN8jxqAFA0DA02H", "Altro Nome")
    assert not p.is_favorite_artist("z", "Sconosciuto")
    assert p.is_blacklisted_artist("q", "artista escluso")

    def release(title, *artists):
        return {"title": title, "genres": [], "artists": [{"id": i, "name": n} for i, n in artists]}

    # parola nel titolo: conta solo come parola intera
    assert "parola nel titolo" in p.blacklist_reason(release("Song (8D Audio)", ("1", "Qualcuno")))
    assert p.blacklist_reason(release("Discover Me", ("1", "Qualcuno"))) is None
    # genere in blacklist assegnato a mano all'artista
    assert "genere in blacklist" in p.blacklist_reason(release("Brano", ("2", "Artista Duro")))
    # collaborazione: basta un artista in blacklist
    reason = p.blacklist_reason(release("Brano", ("3", "Artista Escluso"), ("4", "Ospite")))
    assert reason and "artista in blacklist" in reason
    # genere preferito
    pop = release("Brano", ("5", "Artista Pop"))
    assert p.has_favorite_genre(pop) and p.genres_for_release(pop) == ["pop"]
    assert p.blacklist_reason(pop) is None

    # confronto con gli artisti del database
    report = p.match_report([{"id": "x", "name": "Beyoncé"}, {"id": "y", "name": "Altro"}])
    assert report["favorites"][0][0] == ("Beyoncé", ["Beyoncé"]), report
    assert report["favorites"][1] == ["4x1nvY2FN8jxqAFA0DA02H"], report

# cartella vuota: nessuna regola, nessun errore
with tempfile.TemporaryDirectory() as tmp:
    assert load_profile(tmp).summary()["favorite_artists"] == 0

# nome senza virgolette che YAML legge come "vero": errore chiaro
with tempfile.TemporaryDirectory() as tmp:
    write(tmp, "preferences.yaml", "favorite_artists:\n  - Yes\n")
    try:
        load_profile(tmp)
    except ProfileError as exc:
        assert "virgolette" in str(exc)
    else:
        raise AssertionError("avrebbe dovuto segnalare l'errore")

print("OK: profilo funzionante")