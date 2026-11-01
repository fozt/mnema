"""The README screenshot state: the real panel in each language, and PNGs without metadata."""

from __future__ import annotations

import json
import re
import struct
import zlib

from mnema.bar.protocol import StateMsg
from scripts.screenshots import showcase, strip_png

_CYRILLIC = re.compile("[Ѐ-ӿ]")


def _json(lang: str) -> str:
    return json.dumps(showcase(lang).model_dump(mode="json"), ensure_ascii=False)


def test_english_showcase_has_no_russian_and_russian_one_is_translated():
    english, russian = _json("en"), _json("ru")
    assert not _CYRILLIC.search(english)
    panel = StateMsg.model_validate_json(russian).panel
    assert panel.idle is not None and panel.idle.start.title == "Начать запись"
    assert [(s.label, s.on) for s in panel.idle.sources] == [
        ("mic", True),
        ("system", True),
        ("screen", True),
    ]
    assert [c.detail for c in panel.cards if c.busy] == ["Распознаю речь… 40\xa0%"]
    assert not any(c.detail_warn for c in panel.cards)
    assert panel.settings is not None
    record = next(t for t in panel.settings.tabs if t.id == "record")
    titles = [f.title for s in record.sections for f in s.fields]
    assert {"Экран", "Качество видео", "Распознавание", "Движок", "Модель"} <= set(titles)


def test_strip_png_keeps_the_image_and_drops_metadata():
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + body))
        return struct.pack(">I", len(body)) + kind + body + crc

    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    pixels = chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff"))
    image = b"\x89PNG\r\n\x1a\n" + header + pixels + chunk(b"IEND", b"")
    note = chunk(b"tEXt", b"Source\x00/Users/someone/state.json")
    with_note = image[:33] + note + image[33:]
    assert strip_png(with_note) == image
