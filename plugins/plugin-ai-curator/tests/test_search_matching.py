"""Keyword matching rules for archive search."""

from store import _keyword_in_haystack


def test_latin_terms_match_word_boundaries_not_ids_or_urls() -> None:
    assert _keyword_in_haystack("kv", "https://www.reddit.com/r/x/comments/tkvy5n") is False
    assert _keyword_in_haystack("kv", "the kv cache eviction boundary") is True
    assert _keyword_in_haystack("爵士", "冷静器乐爵士") is True
