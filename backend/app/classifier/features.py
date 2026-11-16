"""Surface-level feature extraction (counts and structure)."""

import re

from app.classifier.lexicons import CODE_BLOCK_REGEX, CODE_LINE_PATTERNS, INLINE_CODE_REGEX
from app.classifier.types import QueryFeatures

_WORD = re.compile(r"\b[\w'+#.-]+\b")
_SENTENCE_END = re.compile(r"[.!?]+(?:\s|$)")
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S", re.MULTILINE)


def _count_code_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() and any(p.search(line) for p in CODE_LINE_PATTERNS))


def extract_features(text: str) -> QueryFeatures:
    words = _WORD.findall(text)
    has_code_block = len(CODE_BLOCK_REGEX.findall(text)) >= 2
    code_line_count = _count_code_lines(text)
    inline_code_count = len(INLINE_CODE_REGEX.findall(text))
    sentences = max(1, len(_SENTENCE_END.findall(text))) if words else 0

    return QueryFeatures(
        word_count=len(words),
        char_count=len(text),
        sentence_count=sentences,
        question_count=text.count("?"),
        line_count=len([ln for ln in text.splitlines() if ln.strip()]),
        list_item_count=len(_LIST_ITEM.findall(text)),
        has_code_block=has_code_block,
        code_line_count=code_line_count,
        inline_code_count=inline_code_count,
        has_code=has_code_block or code_line_count >= 2,
    )
