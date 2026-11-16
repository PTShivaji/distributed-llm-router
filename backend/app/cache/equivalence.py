"""Equivalence guard for approximate (semantic) cache hits.

A similarity score alone cannot tell "same question" from "same question with one
critical detail changed". Measured with the local lexical embedder at the 0.92
threshold, 14 of 16 meaning-changing edits still scored >= 0.92 (negations,
changed numbers, operators, dates, versions, ports, sizes, swapped entities).

This guard extracts the *critical tokens* of a query - the tokens whose change
almost always changes the answer - and a semantic hit is only allowed between
queries with the SAME ordered critical-token sequence:

  numbers / numeric expressions   15, 3.5, 3.12, 10.0.0.5, 2024-01-02, 12:30, 1,000, 1M, 16GB, 50%
  arithmetic / comparison symbols + - * / ^ = < > % x ÷
  negations                       not, no, never, without, doesn't, isn't, cannot, ...
  polarity / direction words      enable/disable, allow/deny, add/remove, before/after, min/max, ...
  number words, months, weekdays, relative dates (today, tomorrow, ...)

Only a short SHA-256 digest of that sequence is stored (never the tokens or the
prompt). Known limitation, by design: the guard does NOT detect changed
entities or word order ("Python" vs "Java", "PostgreSQL to MySQL" vs "MySQL to
PostgreSQL"). That gap is why approximate hits are disabled by default for the
lexical embedder (see SemanticCache / CACHE_SEMANTIC_ALLOW_LEXICAL).
"""

import hashlib
import re

_TOKEN = re.compile(
    r"\d+(?:[.,:/\-]\d+)*[a-z%]*"  # numbers incl. decimals, versions, IPs, dates, times, units (16gb, 1m, 50%)
    r"|[a-z]+"                      # words (apostrophes removed beforehand: doesn't -> doesnt)
    r"|[+\-*/^=<>%×÷]"              # arithmetic / comparison symbols
)

NEGATIONS = frozenset({
    "not", "no", "never", "none", "nobody", "nothing", "nowhere", "neither", "nor", "without", "cannot", "cant",
    "dont", "doesnt", "didnt", "isnt", "arent", "wasnt", "werent", "wont", "wouldnt", "shouldnt", "couldnt",
    "hasnt", "havent", "hadnt", "mustnt", "neednt", "aint", "non", "false", "deny", "denied",
})
POLARITY = frozenset({
    # actions with an opposite
    "enable", "enabled", "disable", "disabled", "allow", "allowed", "block", "blocked", "accept", "reject",
    "add", "remove", "insert", "delete", "create", "destroy", "start", "stop", "open", "close", "lock", "unlock",
    "encrypt", "decrypt", "encode", "decode", "include", "exclude", "install", "uninstall", "import", "export",
    "upload", "download", "push", "pull", "increase", "decrease", "raise", "lower", "grant", "revoke",
    "on", "off", "up", "down", "in", "out", "true", "yes", "valid", "invalid", "public", "private",
    "success", "failure", "pass", "fail", "read", "write", "input", "output", "inbound", "outbound",
    "ascending", "descending", "asc", "desc", "sync", "async", "synchronous", "asynchronous",
    # comparison / ordering / quantity
    "more", "less", "fewer", "greater", "smaller", "larger", "bigger", "higher", "lower", "faster", "slower",
    "before", "after", "above", "below", "over", "under", "between", "first", "last", "next", "previous",
    "min", "max", "minimum", "maximum", "least", "most", "only", "except", "all", "any", "every", "each",
    "some", "both", "either", "plus", "minus", "times", "divided", "multiplied", "per", "than", "vs", "versus",
})
NUMBER_WORDS = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
    "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million", "billion",
    "trillion", "half", "quarter", "double", "triple", "once", "twice", "dozen",
})
DATES = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "today", "tomorrow", "yesterday", "tonight", "now", "weekly", "daily", "monthly", "yearly", "annual",
    "morning", "evening", "night", "am", "pm", "utc", "gmt",
})
CRITICAL_WORDS = NEGATIONS | POLARITY | NUMBER_WORDS | DATES


def tokens(text: str) -> list[str]:
    lowered = text.casefold().replace("'", "").replace("’", "")
    return _TOKEN.findall(lowered)


def has_word_content(text: str) -> bool:
    """False for empty, whitespace-only, punctuation-only or emoji-only queries."""
    return any(t[0].isalpha() for t in tokens(text))


def critical_tokens(text: str) -> list[str]:
    out = []
    for t in tokens(text):
        if t[0].isdigit() or t in CRITICAL_WORDS or (len(t) == 1 and not t.isalnum()):
            out.append(t)
    return out


def critical_signature(text: str) -> str:
    """Digest of the ordered critical-token sequence (stored instead of the tokens themselves)."""
    joined = "\x1f".join(critical_tokens(text))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
