"""Technical-domain and coding-query detection."""

from app.classifier.lexicons import ADVANCED_TECH_TERMS, CODING_REQUEST_PATTERNS, DOMAIN_REGEXES
from app.classifier.types import QueryFeatures, TechnicalResult


def detect_technical(text: str, features: QueryFeatures) -> TechnicalResult:
    terms: set[str] = set()
    domains: list[str] = []
    for domain, regex in DOMAIN_REGEXES.items():
        found = {m.group(0).lower() for m in regex.finditer(text)}
        if found:
            domains.append(domain)
            terms |= found

    coding_request = any(p.search(text) for p in CODING_REQUEST_PATTERNS)
    is_coding = features.has_code or coding_request
    if is_coding and "programming" not in domains:
        domains.insert(0, "programming")

    advanced = len(terms & ADVANCED_TECH_TERMS)
    score = min(1.0, 0.15 * len(terms) + 0.10 * advanced + (0.35 if is_coding else 0.0))

    return TechnicalResult(
        is_technical=is_coding or bool(terms),
        is_coding=is_coding,
        score=round(score, 3),
        domains=tuple(domains),
        detected_terms=tuple(sorted(terms)),
    )
