"""Pure search, pagination, and deterministic Czech collation contracts."""

from dataclasses import dataclass
import re
from typing import Optional, Tuple

from unidecode import unidecode


_BROWSING_SENTINEL = "%#NONE#%"
_EPISODE_PATTERN = re.compile(r"S(\d{1,2})E(\d{1,2})", re.IGNORECASE)
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_CZECH_ACCENT_RANK = {
    "á": 1,
    "č": 1,
    "ď": 1,
    "é": 1,
    "ě": 2,
    "í": 1,
    "ň": 1,
    "ó": 1,
    "ř": 1,
    "š": 1,
    "ť": 1,
    "ú": 1,
    "ů": 2,
    "ý": 1,
    "ž": 1,
}


@dataclass(frozen=True)
class SearchQuery:
    api_text: str
    tokens: Tuple[str, ...]
    browsing: bool


def _normalize_episode_notation(text):
    def replace(match):
        return "S{0}E{1}".format(
            match.group(1).zfill(2), match.group(2).zfill(2)
        )

    return _EPISODE_PATTERN.sub(replace, text)


def _tokenize(text):
    ascii_text = unidecode(_normalize_episode_notation(text)).casefold()
    return tuple(_TOKEN_PATTERN.findall(ascii_text))


def parse_query(text: str) -> SearchQuery:
    """Parse user input without confusing empty searches with browsing."""
    api_text = _normalize_episode_notation(text.strip())
    browsing = text.strip() == _BROWSING_SENTINEL
    if browsing:
        return SearchQuery(api_text="", tokens=(), browsing=True)
    return SearchQuery(api_text=api_text, tokens=_tokenize(api_text), browsing=False)


def matches_filename(query: SearchQuery, filename: str) -> bool:
    """Match every normalized query token, except in explicit browse mode."""
    if query.browsing:
        return True
    if not query.tokens:
        return False

    filename_tokens = _tokenize(filename)
    normalized_filename = "".join(filename_tokens)
    return all(token in normalized_filename for token in query.tokens)


def page_offsets(
    offset: int, limit: int, raw_count: int
) -> Tuple[Optional[int], Optional[int]]:
    """Return previous/next API offsets from the unfiltered result count."""
    previous = max(0, offset - limit) if offset > 0 else None
    next_offset = offset + limit if raw_count >= limit else None
    return previous, next_offset


def czech_sort_key(text: str) -> tuple:
    """Return a locale-independent key with Czech accents and ch after h."""
    folded = text.casefold()
    primary = []
    index = 0
    while index < len(folded):
        if folded.startswith("ch", index):
            # U+FFFF follows every Latin letter while keeping the h prefix.
            primary.append(("h\uffff", 0))
            index += 2
            continue
        character = folded[index]
        primary.append(
            (unidecode(character).casefold(), _CZECH_ACCENT_RANK.get(character, 0))
        )
        index += 1
    return tuple(primary), unidecode(text).casefold(), text
