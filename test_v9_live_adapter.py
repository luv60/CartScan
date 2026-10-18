"""Regression tests for the production V9 catalog adapter."""

from types import SimpleNamespace
import threading

from quickcompare_v9_live import V9LiveMatcher


def test_candidate_ids_include_v7_normalized_provider_id():
    """Raw offers must match V7-normalized catalog records by provider ID."""
    raw_offer = {"product_id": "zepto-123"}
    v7_catalog_record = {"provider_product_id": "zepto-123"}

    assert V9LiveMatcher._ids(raw_offer) == {"zepto-123"}
    assert V9LiveMatcher._ids(v7_catalog_record) == {"zepto-123"}
    assert V9LiveMatcher._ids(raw_offer) & V9LiveMatcher._ids(v7_catalog_record)


def test_find_candidate_matches_raw_offer_to_v7_normalized_id():
    """V9 must find a retrieved catalog item after V7 changes its ID field."""
    matcher = object.__new__(V9LiveMatcher)
    matcher.lock = threading.RLock()
    matcher.retrievers = {"zepto": object()}
    matcher.products = {"zepto": [{"provider_product_id": "zepto-123", "name": "Example product"}]}
    matcher.retrieval_cache = {"zepto|blinkit-456": [SimpleNamespace(source_index=0)]}

    found = matcher._find_candidate(
        {"product_id": "blinkit-456", "name": "Example product"},
        {"store": "Zepto", "product_id": "zepto-123", "name": "Example product"},
    )

    assert found is not None
    assert found[0]["provider_product_id"] == "zepto-123"
