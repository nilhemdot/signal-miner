"""Company name normalization (spec section 6, step 2)."""
import re
import unicodedata

SUFFIXES = {"inc", "llc", "corp", "corporation", "co", "ltd", "plc", "gmbh", "incorporated", "company"}


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).lower()
    s = re.sub(r"\([^)]*\)", " ", s)          # parentheticals
    s = re.sub(r"[^\w\s]", " ", s)             # punctuation
    tokens = s.split()
    while tokens and tokens[-1] in SUFFIXES:  # trailing legal suffixes only; "labs" and "technologies" stay
        tokens.pop()
    return " ".join(tokens)
