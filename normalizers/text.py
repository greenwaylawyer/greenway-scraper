"""Smart title-case normalization for attorney data.

Handles the ALL-CAPS output common in government/bar CSV exports:
  - Names:    O'BRIEN→O'Brien, MCDONALD→McDonald, SMITH-JONES→Smith-Jones
  - Firms:    LLP, LLC, PC, PA, PLLC, PLC, LTD always stay UPPERCASE
  - Suffixes: JR, SR, II, III, IV, V, ESQ always stay UPPERCASE
  - Ordinals: 1ST→1st, 2ND→2nd, 3RD→3rd, 4TH→4th
  - Cities:   NEW YORK→New York, LOS ANGELES→Los Angeles
"""

import re
from typing import Optional

# Tokens that must always be UPPERCASE regardless of position
_ALWAYS_UPPER: frozenset[str] = frozenset({
    # Legal entity suffixes
    "LLC", "LLP", "PC", "PA", "PLLC", "PLC", "LTD", "LP", "LLLP", "RLLP",
    # Name suffixes
    "JR", "SR", "II", "III", "IV", "V", "VI", "ESQ",
    # Geographic codes (when used as abbreviations)
    "USA", "US",
})

# Ordinal pattern: 1ST, 2ND, 3RD, 4TH, etc.
_ORDINAL_RE = re.compile(r'^(\d+)(ST|ND|RD|TH)$', re.IGNORECASE)

# Mc/Mac Celtic prefix: MCDONALD→McDonald, MACINTYRE→MacIntyre
_MCMAC_RE = re.compile(r'^(Mc|Mac)([A-Z])', re.IGNORECASE)


def _title_word(word: str) -> str:
    """Convert a single ALL-CAPS word to proper title case with special rules."""
    if not word:
        return word

    upper = word.upper()

    # Always-uppercase tokens
    if upper in _ALWAYS_UPPER:
        return upper

    # Ordinals: 3RD → 3rd
    m = _ORDINAL_RE.match(word)
    if m:
        return m.group(1) + m.group(2).lower()

    # Hyphenated compound: SMITH-JONES → Smith-Jones
    if '-' in word:
        return '-'.join(_title_word(part) for part in word.split('-'))

    # Apostrophe: O'BRIEN → O'Brien, D'AMICO → D'Amico
    if "'" in word:
        parts = word.split("'", 1)
        return parts[0].capitalize() + "'" + parts[1].capitalize()

    # Mc/Mac prefix: MCDONALD → McDonald, MACINTYRE → MacIntyre
    m = _MCMAC_RE.match(word)
    if m and len(word) > len(m.group(0)):
        prefix = m.group(1).capitalize()        # "Mc" or "Mac"
        next_char = m.group(2).upper()          # First letter after prefix (already capitalized)
        rest = word[len(m.group(0)):].lower()   # Remainder — lowercase only
        return f"{prefix}{next_char}{rest}"

    # Default: capitalize first letter
    return word.capitalize()


def title_name(text: Optional[str]) -> Optional[str]:
    """
    Title-case a personal name (first, last, full).

    Applies Mc/Mac, O', hyphen, and always-upper rules.
    Example: "JOHN MICHAEL O'BRIEN JR" → "John Michael O'Brien JR"
    """
    if not text or not text.strip():
        return text
    return ' '.join(_title_word(w) for w in text.split())


def title_firm(text: Optional[str]) -> Optional[str]:
    """
    Title-case a law firm name, keeping legal suffixes uppercase.

    Example: "GREENBERG TRAURIG, LLP" → "Greenberg Traurig, LLP"
    """
    if not text or not text.strip():
        return text
    # Split on spaces only (preserve commas attached to words)
    words = text.split()
    result = []
    for word in words:
        # Strip trailing comma to check the token
        stripped = word.rstrip(',.')
        suffix = word[len(stripped):]
        upper = stripped.upper()
        if upper in _ALWAYS_UPPER:
            result.append(upper + suffix)
        else:
            result.append(stripped.capitalize() + suffix)
    return ' '.join(result)


def title_address_line(text: Optional[str]) -> Optional[str]:
    """
    Title-case a street address line.

    Lowercases ordinals (3RD→3rd), capitalizes street names,
    does NOT apply Mc/Mac (not appropriate for street names).
    Example: "767 3RD AVE RM 2101" → "767 3rd Ave Rm 2101"
    """
    if not text or not text.strip():
        return text
    words = text.split()
    result = []
    for word in words:
        # Numbers stay as-is
        if word.isdigit():
            result.append(word)
            continue
        # Ordinals: 3RD → 3rd
        m = _ORDINAL_RE.match(word)
        if m:
            result.append(m.group(1) + m.group(2).lower())
            continue
        # Default capitalize
        result.append(word.capitalize())
    return ' '.join(result)


def title_city(text: Optional[str]) -> Optional[str]:
    """
    Title-case a city name.

    Example: "NEW YORK" → "New York", "LOS ANGELES" → "Los Angeles"
    """
    if not text or not text.strip():
        return text
    return ' '.join(w.capitalize() for w in text.split())


def title_law_school(text: Optional[str]) -> Optional[str]:
    """
    Title-case a law school field while preserving common abbreviations.

    Example: "LOYOLA LAW SCHOOL; LOS ANGELES CA" -> "Loyola Law School; Los Angeles CA"
    """
    if not text or not text.strip():
        return text

    tokens = re.split(r'(\s+)', text.strip())
    result = []

    for token in tokens:
        if not token or token.isspace():
            result.append(token)
            continue

        # Preserve surrounding punctuation while title-casing the core token.
        leading = re.match(r'^[^A-Za-z0-9]*', token).group(0)
        trailing = re.search(r'[^A-Za-z0-9]*$', token).group(0)
        core = token[len(leading):len(token) - len(trailing) if trailing else len(token)]

        if not core:
            result.append(token)
            continue

        result.append(f"{leading}{_title_word(core)}{trailing}")

    return ''.join(result)
