"""Rule data for the classifier: patterns, keyword lists and weights.

Keeping data separate from logic means rules can be tuned (or later loaded
from a file/database) without touching the scoring code. All regexes are
compiled case-insensitively unless noted.
"""

import re

from app.classifier.types import Intent

FLAGS = re.IGNORECASE


def _c(pattern: str, flags: int = FLAGS) -> re.Pattern[str]:
    return re.compile(pattern, flags)


def term_regex(terms: list[str]) -> re.Pattern[str]:
    """Match any of `terms` as whole words (works for 'c++', 'node.js', 'ci/cd')."""
    alternation = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![\w]){'(?:' + alternation + ')'}(?![\w])", FLAGS)


# --------------------------------------------------------------------------- #
# Intent patterns: (regex, weight). Each pattern counts at most once.
# Order of the dict also acts as tie-break priority (earlier wins).
# --------------------------------------------------------------------------- #

# An imperative verb at the start of the query states the user's main goal
# ("Design X ... and compare Y" is a design task), so its intent gets a bonus.
# Question words (what/why/how) are deliberately excluded: they are ambiguous.
LEADING_VERB_BONUS = 1.5
_LEAD = r"^\s*(?:please\s+|can you\s+|could you\s+)?"
LEADING_VERBS: dict[Intent, re.Pattern[str]] = {
    Intent.SYSTEM_DESIGN: _c(_LEAD + r"(design|architect)\b"),
    Intent.ANALYSIS: _c(_LEAD + r"(compare|analy[sz]e|evaluate|assess|critique|review)\b"),
    Intent.DEBUGGING: _c(_LEAD + r"(debug|fix|troubleshoot|diagnose)\b"),
    Intent.CODE_GENERATION: _c(_LEAD + r"(implement|refactor|code)\b"),
    Intent.EXPLANATION: _c(_LEAD + r"(explain|describe|clarify)\b"),
    Intent.SUMMARIZATION: _c(_LEAD + r"(summari[sz]e|condense)\b"),
    Intent.TRANSLATION: _c(_LEAD + r"translate\b"),
    Intent.MATH: _c(_LEAD + r"(solve|calculate|compute|prove|derive)\b"),
    Intent.PLANNING: _c(_LEAD + r"(plan|outline a plan)\b"),
}

INTENT_PATTERNS: dict[Intent, list[tuple[re.Pattern[str], float]]] = {
    Intent.DEBUGGING: [
        (_c(r"\b(debug|bug|traceback|stack ?trace|segfault|null pointer|core dump)\b"), 1.5),
        (_c(r"\b(error|exception|crash(es|ed|ing)?|fails?|failing|failed|broken)\b"), 1.0),
        (_c(r"\b(not working|doesn'?t work|isn'?t working|throws?|raises?)\b"), 1.0),
        (_c(r"\b(deadlock|race condition|memory leak|hangs|freezes|infinite loop)\b"), 1.0),
        (_c(r"\bfix(ing)?\b"), 1.0),
        (_c(r"\bwhy (does|is|am|do|did)\b.{0,60}\b(fail|error|crash|wrong|not|slow)"), 1.0),
        (_c(r"\b\w+(Error|Exception):", 0), 1.5),  # e.g. "KeyError:" (case-sensitive)
    ],
    Intent.CODE_GENERATION: [
        (
            _c(
                r"\b(write|create|generate|build|implement|code|make)\b.{0,50}"
                r"\b(function|class|script|program|code|api|endpoint|module|method|"
                r"query|component|app|application|service|cli|unit tests?|regex|decorator)\b"
            ),
            2.0,
        ),
        (_c(r"\bimplement(ation|ing)?\b"), 1.0),
        (_c(r"\b(refactor|rewrite this|convert this code)\b"), 1.5),
        (_c(r"\b(in|using) (python|javascript|typescript|java|c\+\+|rust|golang|sql|bash)\b"), 0.5),
    ],
    Intent.SYSTEM_DESIGN: [
        (_c(r"\b(system design|design (a|an|the)|architect(ure)?|blueprint)\b"), 2.0),
        (
            _c(
                r"\b(scalable|highly available|fault[- ]tolerant|distributed|multi-region)\b.{0,60}"
                r"\b(system|service|platform|architecture|gateway|limiter|cache|queue|pipeline|database)\b"
            ),
            1.0,
        ),
    ],
    Intent.ANALYSIS: [
        (_c(r"\b(compare|comparison|versus|vs\.?|contrast)\b"), 1.5),
        (_c(r"\b(evaluate|assess|analy[sz]e|analysis|critique|review)\b"), 1.2),
        (_c(r"\b(trade-?offs?|pros and cons|advantages and disadvantages|which is better)\b"), 1.5),
    ],
    Intent.MATH: [
        (_c(r"\b(calculate|compute|solve|equation|integral|derivative|theorem|probability|matrix|factorial)\b"), 1.2),
        (_c(r"\b(prove|proof)\b"), 1.0),
        (_c(r"\d+(\.\d+)?\s*[-+*/^×÷]\s*\d+"), 2.0),
    ],
    Intent.SUMMARIZATION: [
        (_c(r"\b(summari[sz]e|summary|tl;?dr|condense|key points|brief overview)\b"), 2.0),
    ],
    Intent.TRANSLATION: [
        (_c(r"\btranslat(e|ion)\b"), 2.0),
        (_c(r"\b(in|into|to) (spanish|french|german|hindi|chinese|japanese|italian|portuguese|"
            r"russian|arabic|korean|marathi|tamil|telugu|bengali|english)\b"), 0.8),
    ],
    Intent.CREATIVE_WRITING: [
        (_c(r"\b(write|compose|create)\b.{0,30}\b(poem|story|song|haiku|limerick|lyrics|novel|joke|essay|blog post|tagline|slogan)\b"), 2.0),
        (_c(r"\b(poem|story|haiku|limerick|fiction|rhyme)\b"), 0.8),
    ],
    Intent.PLANNING: [
        (_c(r"\b(plan|roadmap|itinerary|schedule|strategy|migration path)\b"), 1.2),
        (_c(r"\bhow (should|do|can) i (start|approach|get started|prepare|learn)\b"), 1.0),
    ],
    Intent.EXPLANATION: [
        (_c(r"\b(explain|describe|clarify|elaborate|walk me through|help me understand)\b"), 1.5),
        (_c(r"\bhow (does|do|did|is|are|can|would)\b"), 1.0),
        (_c(r"\bwhy (does|do|is|are|did|would)\b"), 1.0),
        (_c(r"\b(what('s| is| are) the|explain the) (difference|reason|purpose|idea|point)\b"), 0.8),
    ],
    Intent.QUESTION_ANSWERING: [
        (_c(r"^\s*(what|who|when|where|which|is|are|does|can)\b"), 0.8),
        (_c(r"\b(what is|what's|what are|who is|who was|when did|when was|where is|"
            r"define|definition of|meaning of|capital of|how many|how much)\b"), 1.0),
    ],
    Intent.CONVERSATION: [
        (_c(r"^\s*(hi|hello|hey|yo|thanks|thank you|good (morning|afternoon|evening)|greetings)\b"), 2.0),
        (_c(r"\b(how are you|what'?s up|nice to meet you)\b"), 1.5),
    ],
}

# Baseline reasoning demand of each intent (added to the complexity score).
INTENT_COMPLEXITY_PRIOR: dict[Intent, float] = {
    Intent.CONVERSATION: 0.00,
    Intent.QUESTION_ANSWERING: 0.02,
    Intent.TRANSLATION: 0.05,
    Intent.SUMMARIZATION: 0.08,
    Intent.CREATIVE_WRITING: 0.10,
    Intent.EXPLANATION: 0.10,
    Intent.GENERAL: 0.08,
    Intent.MATH: 0.14,
    Intent.PLANNING: 0.14,
    Intent.CODE_GENERATION: 0.16,
    Intent.DEBUGGING: 0.18,
    Intent.ANALYSIS: 0.20,
    Intent.SYSTEM_DESIGN: 0.26,
}

# --------------------------------------------------------------------------- #
# Reasoning complexity markers: (regex, weight, label)
# --------------------------------------------------------------------------- #
REASONING_MARKERS: list[tuple[re.Pattern[str], float, str]] = [
    (_c(r"\bwhy\b"), 0.05, "causal 'why' reasoning"),
    (_c(r"\b(step[- ]by[- ]step|walk me through|in detail|detailed|thoroughly|in depth)\b"), 0.07, "requests detailed / step-by-step reasoning"),
    (_c(r"\b(compare|comparison|versus|vs\.?|contrast|differences? between|distinguish)\b"), 0.08, "comparison of alternatives"),
    (_c(r"\b(when (to|should (i|we|you)) (use|choose|pick)|which (one )?should)\b"), 0.06, "decision guidance"),
    (_c(r"\b(trade-?offs?|pros and cons|advantages and disadvantages)\b"), 0.10, "trade-off analysis"),
    (_c(r"\b(analy[sz]e|evaluate|assess|critique|justify)\b"), 0.08, "analytical evaluation"),
    (_c(r"\b(prove|proof|derive|derivation)\b"), 0.12, "formal proof / derivation"),
    (_c(r"\b(optimi[sz]e|optimal|minimi[sz]e|maximi[sz]e|efficient(ly)?|performance)\b"), 0.07, "optimization goal"),
    (_c(r"\b(design|architect(ure)?)\b"), 0.09, "design reasoning"),
    (_c(r"\b(scal(e|able|ability|ing)|fault[- ]toleran(t|ce)|high availability|consisten(t|cy)|concurren(t|cy)|distributed)\b"), 0.10, "systems-level concerns"),
    (_c(r"\b(implications?|consequences?|impact|root cause)\b"), 0.06, "impact / root-cause analysis"),
    (_c(r"\b(edge cases?|corner cases?|failure modes?)\b"), 0.06, "edge-case consideration"),
    (_c(r"\b(strateg(y|ies)|approach(es)?|best way)\b"), 0.04, "choosing an approach"),
    (_c(r"\b(what if|hypothetical(ly)?|suppose)\b"), 0.05, "counterfactual reasoning"),
]
REASONING_MARKER_CAP = 0.35

CONSTRAINT_REGEX = _c(
    r"\b(must|should|need to|needs to|has to|without|ensure|guarantee|at least|at most|"
    r"no more than|within|constraints?|requirements?|handl(e|es|ing)|support(s|ing)?|while (also )?)\b"
)
CONSTRAINT_WEIGHT = 0.03
CONSTRAINT_CAP = 0.12

SEQUENCE_REGEX = _c(r"\b(then|also|additionally|furthermore|finally|first(ly)?|second(ly)?|next|after that)\b")

SIMPLICITY_MARKERS: list[tuple[re.Pattern[str], float, str]] = [
    (_c(r"^\s*(what is|what's|who is|who was|when (is|was|did)|where is|define|meaning of|how many)\b"), -0.08, "short factual question form"),
    (_c(r"^\s*(hi|hello|hey|thanks|thank you|good (morning|afternoon|evening))\b"), -0.10, "greeting / small talk"),
    (_c(r"\b(quick(ly)?|simple|briefly|in one (word|sentence|line)|short answer|just tell me)\b"), -0.05, "explicitly asks for a short/simple answer"),
]

# --------------------------------------------------------------------------- #
# Technical domain vocabularies
# --------------------------------------------------------------------------- #
TECH_DOMAINS: dict[str, list[str]] = {
    "programming": [
        "python", "javascript", "typescript", "java", "c++", "c#", "rust", "golang", "kotlin",
        "swift", "ruby", "php", "function", "functions", "recursion", "compiler", "runtime",
        "syntax", "library", "framework", "async", "await", "regex", "json", "yaml",
        "decorator", "generator", "iterator", "closure", "oop", "pointer", "pointers", "csv",
        "threads", "threading", "multiprocessing", "coroutine", "coroutines",
    ],
    "web": [
        "api", "apis", "rest", "restful", "graphql", "http", "https", "fastapi", "django",
        "flask", "react", "vue", "angular", "node.js", "nodejs", "frontend", "backend",
        "endpoint", "endpoints", "websocket", "cors", "html", "css",
    ],
    "databases": [
        "sql", "postgres", "postgresql", "mysql", "sqlite", "mongodb", "redis", "database",
        "databases", "query optimization", "orm", "sqlalchemy", "transactions", "nosql",
        "sharding", "replication", "join", "joins",
    ],
    "devops": [
        "docker", "kubernetes", "k8s", "ci/cd", "terraform", "ansible", "aws", "azure", "gcp",
        "deployment", "helm", "nginx", "linux", "bash", "powershell", "devops",
    ],
    "ai_ml": [
        "machine learning", "deep learning", "neural network", "neural networks", "llm", "llms",
        "transformer", "transformers", "embedding", "embeddings", "fine-tuning", "fine-tune",
        "pytorch", "tensorflow", "gradient descent", "rag", "vector database", "tokenizer",
        "inference", "model training", "backpropagation",
    ],
    "systems": [
        "distributed", "concurrency", "multithreading", "latency", "throughput", "cache",
        "caching", "load balancer", "load balancing", "consensus", "microservice",
        "microservices", "message queue", "kafka", "rabbitmq", "rate limiter", "rate limiting",
        "fault tolerance", "scalability", "race condition", "deadlock", "eventual consistency",
        "cap theorem", "raft", "paxos",
    ],
    "security": [
        "authentication", "authorization", "oauth", "jwt", "encryption", "tls", "ssl", "xss",
        "csrf", "sql injection", "vulnerability", "hashing",
    ],
    "algorithms": [
        "algorithm", "algorithms", "big o", "time complexity", "space complexity",
        "binary search", "dynamic programming", "sorting", "linked list", "hash map", "hashmap",
        "hash table", "binary tree", "priority queue", "bfs", "dfs", "graph traversal",
    ],
}

# Terms implying deeper technical reasoning (extra complexity weight).
ADVANCED_TECH_TERMS: set[str] = {
    "distributed", "consensus", "concurrency", "race condition", "deadlock", "sharding",
    "replication", "fault tolerance", "scalability", "microservices", "kubernetes",
    "load balancing", "load balancer", "rate limiter", "rate limiting", "message queue",
    "kafka", "dynamic programming", "gradient descent", "transformer", "transformers",
    "fine-tuning", "vector database", "transactions", "query optimization",
    "eventual consistency", "cap theorem", "raft", "paxos", "backpropagation",
    "multithreading",
}

DOMAIN_REGEXES: dict[str, re.Pattern[str]] = {d: term_regex(t) for d, t in TECH_DOMAINS.items()}

# --------------------------------------------------------------------------- #
# Code detection
# --------------------------------------------------------------------------- #
CODE_BLOCK_REGEX = re.compile(r"```")
INLINE_CODE_REGEX = re.compile(r"`[^`\n]+`")

# Applied per line. Mostly case-sensitive: code keywords are lower-case.
CODE_LINE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*(def|class|import|return|async def|function|const|let|var|public|private|fn|func|package|#include)\b"),
    re.compile(r"^\s*from\s+[\w.]+\s+import\b"),
    re.compile(r"[;{}]\s*$"),
    re.compile(r"\b[A-Za-z_]\w*\([^()]*\)\s*[:{;]?\s*$"),
    re.compile(r"(=>|->|==|!=|\+=|::|&&|\|\|)"),
    re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE TABLE|ALTER TABLE)\b"),
    re.compile(r"^(Traceback \(most recent call last\)|\s*File \".*\", line \d+)"),
    re.compile(r"\b\w+(Error|Exception):"),
]

CODING_REQUEST_PATTERNS: list[re.Pattern[str]] = [
    _c(
        r"\b(write|create|implement|generate|build|refactor|debug|fix|optimi[sz]e|review)\b.{0,50}"
        r"\b(function|class|method|script|program|code|endpoint|api|module|unit tests?|query|regex|algorithm|bug)\b"
    ),
    _c(r"\b(in|using) (python|javascript|typescript|java|c\+\+|rust|golang|sql|bash)\b"),
    _c(r"\b(compile|compiler error|syntax error|stack ?trace|traceback|pull request|unit tests?|code review)\b"),
]
