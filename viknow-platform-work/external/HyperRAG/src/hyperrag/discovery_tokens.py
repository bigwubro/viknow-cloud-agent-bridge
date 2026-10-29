"""Versioned, model-free directory tokenization, independent of BM25 runtime state."""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from functools import lru_cache

import jieba

# Only grammatical particles; domain words and short identifiers remain searchable.
_PARTICLES = frozenset(
    [
        "的",
        "了",
        "在",
        "是",
        "有",
        "和",
        "与",
        "也",
        "其",
        "此",
        "该",
        "等",
        "呢",
        "其中",
        "如下",
        "什么",
        "如果",
    ]
)
_PARTS = re.compile(r"[\u4e00-\u9fff]+|[a-z0-9]+(?:[-./&+_][a-z0-9]+)*")


def normalize_search_text(*values: object) -> str:
    combined = " ".join(str(value or "") for value in values)
    normalized = unicodedata.normalize("NFKC", combined).casefold()
    normalized = re.sub(r"(?<=[a-z0-9])(?=[\u4e00-\u9fff])", " ", normalized)
    normalized = re.sub(r"(?<=[\u4e00-\u9fff])(?=[a-z0-9])", " ", normalized)
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


@lru_cache(maxsize=1)
def _resources() -> tuple[jieba.Tokenizer, str]:
    # A private dictionary prevents unrelated jieba.add_word calls changing the index.
    tokenizer = jieba.Tokenizer()
    with tokenizer.get_dict_file() as dictionary:
        digest = hashlib.sha256(dictionary.read()).hexdigest()
    tokenizer.cache_file = (
        f"hyperrag-directory-jieba-{os.getuid()}-{jieba.__version__}-{digest}.cache"
    )
    policy = hashlib.sha256(" ".join(sorted(_PARTICLES)).encode()).hexdigest()[:12]
    version = f"directory-v1:precise:hmm-off:jieba-{jieba.__version__}:{digest}:{policy}"
    return tokenizer, version


def discovery_token_version() -> str:
    return _resources()[1]


@lru_cache(maxsize=4096)
def discovery_tokens(text: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: set[str] = set()
    for part in _PARTS.findall(normalized):
        if "\u4e00" <= part[0] <= "\u9fff":
            tokens.update(_resources()[0].cut(part, HMM=False))
        else:
            tokens.add(part)
            tokens.update(re.findall(r"[a-z0-9]+", part))
    return frozenset(token for token in tokens if token and token not in _PARTICLES)


def query_terms(query: str) -> tuple[list[str], list[str]]:
    tokens = sorted(discovery_tokens(query))
    if not tokens:
        raise ValueError("Discovery query must contain searchable words or identifiers")
    patterns = []
    for token in tokens:
        if token.isascii() and len(token) >= 3:
            literal = normalize_search_text(token)
            literal = literal.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            patterns.append(f"%{literal}%")
    return tokens, sorted(set(patterns))
