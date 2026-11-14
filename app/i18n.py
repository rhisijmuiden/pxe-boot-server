"""Translations. English is the default, Dutch is complete. Add a language by adding locales/<code>.json."""
from __future__ import annotations

import json
from functools import lru_cache

from . import config

LANGS = {"en": "English", "nl": "Nederlands"}


@lru_cache
def catalog(lang: str) -> dict[str, str]:
    try:
        return json.loads((config.APP_DIR / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def translate(lang: str, key: str, **kw) -> str:
    text = catalog(lang).get(key) or catalog("en").get(key) or key
    if kw:
        try:
            text = text.format(**kw)
        except (KeyError, IndexError, ValueError):
            pass
    return text


def pick(request, user=None) -> str:
    from . import db
    lang = request.cookies.get("lang")
    if user is not None and user["lang"]:
        lang = user["lang"]
    if lang not in LANGS:
        lang = db.get_setting("default_lang") or "en"
    return lang if lang in LANGS else "en"
