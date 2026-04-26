"""Internationalization module — UI strings and card name translation.

Provides a singleton translator that loads translations from an external JSON
file. Card names are translated using Scryfall ``printed_name`` data, with the
neural network always operating on English names internally.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_TRANSLATIONS_FILE = Path(__file__).parent / "translations.json"
_CARD_CACHE_DIR = Path(__file__).parent / "card_cache"

# Scryfall ``lang`` codes that map to our language keys.
# See https://scryfall.com/docs/api/languages
SCRYFALL_LANG_MAP: dict[str, str] = {
    "en": "en",
    "fr": "fr",
    "es": "es",
    "de": "de",
    "pt": "pt",
    "it": "it",
    "ja": "ja",
    "ko": "ko",
    "zh-Hans": "zhs",
}

# Human-readable display names for each supported language.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "de": "Deutsch",
    "pt": "Português",
    "it": "Italiano",
    "ja": "日本語",
    "ko": "한국어",
    "zh-Hans": "简体中文",
}


class Translator:
    """Singleton translator providing UI strings and card name mapping.

    Args:
        language: ISO language code (e.g. ``"en"``, ``"fr"``).
    """

    _instance: Translator | None = None
    _language: str = "en"
    _strings: dict[str, dict[str, str]] = {}
    _card_names_en_to_local: dict[str, str] = {}
    _card_names_local_to_en: dict[str, str] = {}

    def __init__(self) -> None:
        raise RuntimeError("Use Translator.instance() instead")

    @classmethod
    def instance(cls) -> Translator:
        """Return the global translator singleton."""
        if cls._instance is None:
            inst = object.__new__(cls)
            cls._instance = inst
            inst._load_strings()
        return cls._instance

    def _load_strings(self) -> None:
        """Load UI string translations from the JSON file."""
        if self._strings:
            return
        try:
            with open(_TRANSLATIONS_FILE, encoding="utf-8") as f:
                self.__class__._strings = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load translations: %s", exc)
            self.__class__._strings = {}

    @property
    def language(self) -> str:
        return self._language

    def set_language(self, language: str) -> None:
        """Change the active language.

        Args:
            language: ISO language code (must be a key in translations.json).
        """
        if language not in self._strings and language != "en":
            logger.warning("Unknown language %r, falling back to English", language)
            language = "en"
        self.__class__._language = language

    def tr(self, key: str, **kwargs: object) -> str:
        """Translate a UI string key, with optional ``str.format`` interpolation.

        Args:
            key: Translation key (e.g. ``"waiting_for_draft"``).
            **kwargs: Format arguments (e.g. ``count=5``).

        Returns:
            Translated string, or the English fallback, or the raw key.
        """
        lang_strings = self._strings.get(self._language, {})
        text = lang_strings.get(key)
        if text is None:
            # Fall back to English.
            text = self._strings.get("en", {}).get(key)
        if text is None:
            return key
        if kwargs:
            try:
                text = text.format(**{k: v for k, v in kwargs.items()})
            except (KeyError, IndexError):
                pass
        return text

    # -- Card name translation -----------------------------------------------

    def load_card_translations(
        self,
        scryfall_dir: Path,
        set_code: str | None = None,
    ) -> None:
        """Build English↔local card name maps for the active draft set.

        Strategy:
        1. Load cached per-language JSON from ``overlay/i18n/card_cache/``.
        2. If *set_code* is given, only that set is fetched from the Scryfall
           search API when no cache exists.  Otherwise no API calls are made.
        3. Fall back to ``oracle_id`` bridging in the bulk file.

        Args:
            scryfall_dir: Directory containing Scryfall JSON files.
            set_code: Three-letter set code of the current draft (e.g.
                ``"TMT"``).  When ``None``, only cached translations and the
                bulk-file fallback are used.
        """
        target_lang = SCRYFALL_LANG_MAP.get(self._language)
        if not target_lang or target_lang == "en":
            self.__class__._card_names_en_to_local = {}
            self.__class__._card_names_local_to_en = {}
            return

        en_to_local: dict[str, str] = {}
        local_to_en: dict[str, str] = {}

        _CARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Determine which set(s) to load.
        set_codes: list[str] = []
        if set_code:
            set_codes = [set_code.upper()]
        else:
            # No active set — load whatever is already cached (no API calls).
            for cached in _CARD_CACHE_DIR.glob(f"*_{target_lang}.json"):
                code = cached.stem.split("_")[0].upper()
                set_codes.append(code)

        # For each set, load from cache or fetch from Scryfall API.
        for code in set_codes:
            cache_path = _CARD_CACHE_DIR / f"{code.lower()}_{target_lang}.json"
            set_map = self._load_set_cache(cache_path)
            if set_map is None:
                set_map = self._fetch_set_translations(code, target_lang, scryfall_dir)
                if set_map:
                    self._save_set_cache(cache_path, set_map)
            if set_map:
                for en_name, local_name in set_map.items():
                    en_to_local[en_name] = local_name
                    local_to_en[local_name] = en_name

        # Additional fallback: oracle_id bridging from the bulk file.
        if not en_to_local:
            self._oracle_id_fallback(scryfall_dir, target_lang, en_to_local, local_to_en)

        self.__class__._card_names_en_to_local = en_to_local
        self.__class__._card_names_local_to_en = local_to_en
        logger.info(
            "Loaded %d card translations for language %r",
            len(en_to_local),
            self._language,
        )

    @staticmethod
    def _load_set_cache(cache_path: Path) -> dict[str, str] | None:
        """Load a cached English→local card name map, or None if missing."""
        if not cache_path.exists():
            return None
        try:
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _save_set_cache(cache_path: Path, mapping: dict[str, str]) -> None:
        """Write a cached English→local card name map."""
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(mapping, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning("Could not write card cache %s: %s", cache_path, exc)

    @staticmethod
    def _fetch_set_translations(
        set_code: str,
        scryfall_lang: str,
        scryfall_dir: Path,
    ) -> dict[str, str]:
        """Fetch foreign card names for a set from the Scryfall search API.

        Builds an ``oracle_id → English name`` index from the local per-set
        JSON, then queries Scryfall for the same set in the target language
        and maps ``oracle_id → printed_name``.

        Args:
            set_code: Three-letter set code (e.g. ``"TMT"``).
            scryfall_lang: Scryfall language code (e.g. ``"fr"``).
            scryfall_dir: Directory with per-set Scryfall JSONs.

        Returns:
            Dict mapping English card names to foreign card names.
        """
        import httpx

        # Build oracle_id → English name from local data.
        set_path = scryfall_dir / f"{set_code.lower()}_cards.json"
        if not set_path.exists():
            return {}

        try:
            with open(set_path, encoding="utf-8") as f:
                en_cards = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

        oracle_to_en: dict[str, str] = {}
        for card in en_cards:
            oid = card.get("oracle_id")
            name = card.get("name", "")
            if oid and name:
                oracle_to_en[oid] = name

        if not oracle_to_en:
            return {}

        # Query Scryfall search API for the set in the target language.
        # URL: /cards/search?q=set:{code}+lang:{lang}&unique=prints
        mapping: dict[str, str] = {}
        query = f"set:{set_code.lower()}+lang:{scryfall_lang}"
        url = f"https://api.scryfall.com/cards/search?q={query}&unique=prints"

        logger.info("Fetching %s card names for %s from Scryfall...", scryfall_lang, set_code)

        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                while url:
                    resp = client.get(url)
                    if resp.status_code == 404:
                        # No results for this set/language — not an error.
                        break
                    resp.raise_for_status()
                    data = resp.json()

                    for card in data.get("data", []):
                        oid = card.get("oracle_id")
                        printed = card.get("printed_name") or card.get("name", "")
                        if oid and printed and oid in oracle_to_en:
                            en_name = oracle_to_en[oid]
                            mapping[en_name] = printed

                    # Scryfall paginates results.
                    if data.get("has_more"):
                        url = data.get("next_page")
                        # Respect Scryfall rate limit (100ms between requests).
                        time.sleep(0.1)
                    else:
                        url = ""

        except Exception as exc:
            logger.warning(
                "Scryfall API request failed for %s/%s: %s",
                set_code, scryfall_lang, exc,
            )

        logger.info(
            "Fetched %d/%d translations for %s (%s)",
            len(mapping), len(oracle_to_en), set_code, scryfall_lang,
        )
        return mapping

    @staticmethod
    def _oracle_id_fallback(
        scryfall_dir: Path,
        target_lang: str,
        en_to_local: dict[str, str],
        local_to_en: dict[str, str],
    ) -> None:
        """Last-resort: bridge English↔foreign via oracle_id in the bulk file."""
        bulk_path = scryfall_dir / "default_cards.json"
        if not bulk_path.exists():
            return

        try:
            with open(bulk_path, encoding="utf-8") as f:
                cards = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        oracle_en: dict[str, str] = {}
        oracle_foreign: dict[str, str] = {}
        for card in cards:
            oid = card.get("oracle_id")
            if not oid:
                continue
            card_lang = card.get("lang", "en")
            name = card.get("name", "")
            printed = card.get("printed_name", "")
            if card_lang == "en" and name:
                oracle_en.setdefault(oid, name)
            elif card_lang == target_lang and (printed or name):
                oracle_foreign.setdefault(oid, printed or name)

        for oid, foreign_name in oracle_foreign.items():
            en_name = oracle_en.get(oid)
            if en_name and en_name not in en_to_local:
                en_to_local[en_name] = foreign_name
                local_to_en[foreign_name] = en_name

    def card_name(self, english_name: str) -> str:
        """Translate an English card name to the current language.

        Args:
            english_name: Card name in English (as used by the neural network).

        Returns:
            Translated name if available, otherwise the English name unchanged.
        """
        if self._language == "en" or not self._card_names_en_to_local:
            return english_name
        return self._card_names_en_to_local.get(english_name, english_name)

    def to_english(self, local_name: str) -> str:
        """Convert a local-language card name back to English.

        Args:
            local_name: Card name in the current display language.

        Returns:
            English name if a mapping exists, otherwise the input unchanged.
        """
        if self._language == "en" or not self._card_names_local_to_en:
            return local_name
        return self._card_names_local_to_en.get(local_name, local_name)


# -- Module-level convenience function --------------------------------------


def tr(key: str, **kwargs: object) -> str:
    """Shorthand for ``Translator.instance().tr(key, **kwargs)``."""
    return Translator.instance().tr(key, **kwargs)


def card_name(english_name: str) -> str:
    """Shorthand for ``Translator.instance().card_name(english_name)``."""
    return Translator.instance().card_name(english_name)


def to_english(local_name: str) -> str:
    """Shorthand for ``Translator.instance().to_english(local_name)``."""
    return Translator.instance().to_english(local_name)
