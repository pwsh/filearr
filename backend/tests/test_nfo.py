"""Kodi NFO parsing — valid XML, plain text, and malicious (XXE) input."""

from filearr.nfo import parse_nfo_bytes

VALID_MOVIE = b"""<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <title>Dune</title>
  <originaltitle>Dune: Part One</originaltitle>
  <year>2021</year>
  <plot>A noble family becomes embroiled in a war for a desert planet.</plot>
  <genre>Sci-Fi</genre>
  <genre>Adventure</genre>
  <rating value="8.0"/>
  <runtime>155</runtime>
  <uniqueid type="imdb">tt1160419</uniqueid>
</movie>
"""

VALID_EPISODE = b"""<episodedetails>
  <title>Welcome to the Playground</title>
  <season>1</season>
  <episode>1</episode>
  <plot>Orphaned sisters...</plot>
  <aired>2021-11-06</aired>
</episodedetails>
"""


def test_valid_movie_nfo():
    meta = parse_nfo_bytes(VALID_MOVIE)
    assert meta["title"] == "Dune"
    assert meta["year"] == 2021
    assert meta["plot"].startswith("A noble family")
    assert meta["genre"] == ["Sci-Fi", "Adventure"]
    assert meta["rating"] == 8.0
    assert meta["runtime"] == 155
    assert meta["external_ids"]["imdb"] == "tt1160419"
    assert meta["nfo_kind"] == "movie"


def test_valid_episode_nfo():
    meta = parse_nfo_bytes(VALID_EPISODE)
    assert meta["title"] == "Welcome to the Playground"
    assert meta["season"] == 1
    assert meta["episode"] == 1
    assert meta["nfo_kind"] == "episodedetails"


def test_plain_text_nfo_returns_empty():
    # Old Kodi convention: a bare URL or free text in a .nfo file.
    assert parse_nfo_bytes(b"https://www.imdb.com/title/tt1160419/") == {}
    assert parse_nfo_bytes(b"just some notes about this movie") == {}


def test_empty_nfo_returns_empty():
    assert parse_nfo_bytes(b"") == {}
    assert parse_nfo_bytes(b"   \n  ") == {}


def test_unknown_root_ignored():
    assert parse_nfo_bytes(b"<html><body>nope</body></html>") == {}


def test_malicious_xxe_external_entity_is_blocked():
    # Classic XXE: attempt to read /etc/passwd via an external entity.
    payload = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE movie [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
        b"<movie><title>&xxe;</title></movie>"
    )
    meta = parse_nfo_bytes(payload)
    # defusedxml refuses the DTD/entity → we return {} rather than leaking the file.
    assert meta == {}


def test_billion_laughs_entity_bomb_is_blocked():
    payload = (
        b'<?xml version="1.0"?>\n'
        b"<!DOCTYPE lolz [\n"
        b'  <!ENTITY lol "lol">\n'
        b'  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
        b'  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
        b"]>\n"
        b"<movie><title>&lol3;</title></movie>"
    )
    meta = parse_nfo_bytes(payload)
    assert meta == {}


def test_oversize_nfo_rejected():
    big = b"<movie><title>" + b"A" * (600 * 1024) + b"</title></movie>"
    assert parse_nfo_bytes(big) == {}
