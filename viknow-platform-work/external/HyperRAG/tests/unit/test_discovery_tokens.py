import pytest
from hyperrag.discovery_tokens import discovery_token_version, discovery_tokens, query_terms


def test_chinese_tokens_are_words_not_arbitrary_overlapping_fragments():
    tokens = discovery_tokens("热干面的制作方法")
    assert "制作方法" in tokens
    assert "干面" not in tokens
    assert "的" not in tokens
    assert discovery_tokens("刻蚀有哪些工艺") & discovery_tokens("刻蚀工艺手册")


@pytest.mark.parametrize("literal", ["Si", "HBr", "Si+HBr", "LAM-2300", "x_17", "a12-uuid-98"])
def test_identifiers_and_chemistry_are_preserved(literal):
    assert literal.casefold() in discovery_tokens(literal)


def test_single_character_domain_terms_are_not_discarded():
    assert "硅" in discovery_tokens("硅")
    assert "氢" in discovery_tokens("氢")


def test_tokenization_is_stable_and_does_not_depend_on_bm25():
    assert discovery_tokens("ＬＡＭ-2300 硅") == discovery_tokens("硅 lam-2300")
    assert discovery_token_version() == discovery_token_version()


def test_empty_effective_query_is_not_an_enumeration():
    with pytest.raises(ValueError, match="searchable"):
        query_terms("的 在 和")
    with pytest.raises(ValueError, match="searchable"):
        query_terms("???")


def test_short_terms_use_tokens_and_long_english_retains_substrings():
    tokens, patterns = query_terms("硅 Si HBr LAM-2300")
    assert "硅" in tokens and "si" in tokens
    assert "%hbr%" in patterns
    assert "%lam 2300%" in patterns


def test_short_identifier_terms_are_exact_not_english_word_fragments():
    assert query_terms("AI") == (["ai"], [])
    assert "ai" not in discovery_tokens("OpenAI")


def test_dictionary_cache_identity_matches_versioned_dictionary():
    from hyperrag.discovery_tokens import _resources

    tokenizer, version = _resources()
    digest = version.split(":")[-2]
    assert digest in tokenizer.cache_file
