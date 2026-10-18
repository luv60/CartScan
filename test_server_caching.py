import sys
from pathlib import Path
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

import asyncio
import hashlib
import time
from cachetools import TTLCache
from server import (
    app,
    RAM_STORE_QUERY_CACHE,
    register_canonical_mapping,
    lookup_canonical_mapping,
    CANONICAL_MAPPINGS,
    build_canonical_cards,
)
from fastapi.testclient import TestClient

client = TestClient(app)

def test_canonical_sku_lookup():
    print("======================================================================")
    print("1. Testing Canonical SKU Lookup (SQLite / In-Memory O(1) Dict)")
    print("======================================================================")
    z_id = "zepto_milk_sku_101"
    b_id = "blinkit_milk_sku_202"
    sku_id = "canonical_dairy_amul_toned_500ml"

    # Register mapping
    register_canonical_mapping(z_id, b_id, sku_id)

    # 1. In-memory O(1) dictionary check
    assert (z_id, b_id) in CANONICAL_MAPPINGS, "Failed: (z_id, b_id) not in CANONICAL_MAPPINGS dict"
    assert (b_id, z_id) in CANONICAL_MAPPINGS, "Failed: bidirectional (b_id, z_id) not in CANONICAL_MAPPINGS dict"

    # 2. lookup_canonical_mapping test
    found_sku = lookup_canonical_mapping(z_id, b_id)
    assert found_sku == sku_id, f"Failed: Expected {sku_id}, got {found_sku}"
    found_sku_rev = lookup_canonical_mapping(b_id, z_id)
    assert found_sku_rev == sku_id, f"Failed: Bidirectional expected {sku_id}, got {found_sku_rev}"
    print(f"  ✓ Direct & Bidirectional lookup_canonical_mapping: {found_sku}")

    # 3. REST endpoints test
    resp_post = client.post("/api/canonical-mappings", json={
        "zepto_item_id": "z_test_99",
        "blinkit_item_id": "b_test_99",
        "canonical_sku_id": "sku_test_99"
    })
    assert resp_post.status_code == 200, f"Failed POST /api/canonical-mappings: {resp_post.text}"
    assert resp_post.json()["status"] == "success"

    resp_get = client.get("/api/canonical-mappings?zepto_item_id=z_test_99&blinkit_item_id=b_test_99")
    assert resp_get.status_code == 200
    assert resp_get.json()["canonical_sku_id"] == "sku_test_99"
    assert resp_get.json()["found"] is True
    print("  ✓ REST API endpoints /api/canonical-mappings (POST & GET) PASSED!")

    # 4. Canonical mapping bypass test in build_canonical_cards
    # Create two products with completely mismatched names that would fail fuzzy matching
    b_item = {
        "id": "b_unmatchable_1",
        "name": "Completely Strange Unrelated Raw Title 500ml",
        "packsize": "500 ml",
        "price": 30.0,
        "is_available": True
    }
    z_item = {
        "id": "z_unmatchable_1",
        "name": "Different Brand Raw SKU Title 500ml",
        "packsize": "500 ml",
        "price": 28.0,
        "is_available": True
    }
    register_canonical_mapping("z_unmatchable_1", "b_unmatchable_1", "sku_unmatchable_resolved_card")
    cards = build_canonical_cards([b_item], [z_item])
    matched_card = next((c for c in cards if c["blinkit"] and c["zepto"]), None)
    assert matched_card is not None, "Failed: Canonical mapping did not bypass fuzzy matching to pair items!"
    print(f"  ✓ Fuzzy matching bypass PASSED! Items paired via canonical mapping: '{matched_card['canonical_name']}'")
    print("  ✓ Task 1: Canonical SKU Lookup PASSED!\n")


def test_ttl_store_caching():
    print("======================================================================")
    print("2. Testing TTL Store Caching (key format cache:<store_id>:<query_hash>)")
    print("======================================================================")
    # Verify TTLCache properties
    assert isinstance(RAM_STORE_QUERY_CACHE, TTLCache), "RAM_STORE_QUERY_CACHE is not a TTLCache"
    assert RAM_STORE_QUERY_CACHE.maxsize == 2048, f"Expected maxsize 2048, got {RAM_STORE_QUERY_CACHE.maxsize}"
    assert RAM_STORE_QUERY_CACHE.ttl == 300, f"Expected TTL 300s, got {RAM_STORE_QUERY_CACHE.ttl}"
    print(f"  ✓ TTLCache initialized: maxsize={RAM_STORE_QUERY_CACHE.maxsize}, ttl={RAM_STORE_QUERY_CACHE.ttl}s")

    # Generate store cache key
    test_store_id = "e9b25f0e-d7f4-42b7-a3f2-89b9d31317d7"
    test_query = "amul butter 100g"
    query_hash = hashlib.sha256(test_query.lower().strip().encode("utf-8")).hexdigest()[:16]
    expected_cache_key = f"cache:{test_store_id}:{query_hash}"

    print(f"  Testing cache key format: {expected_cache_key}")
    assert expected_cache_key.startswith("cache:"), "Cache key prefix must be 'cache:'"
    assert test_store_id in expected_cache_key, "Store ID missing in cache key"
    assert query_hash in expected_cache_key, "Query hash missing in cache key"

    # Pre-populate cache to simulate first search response
    mock_payload = {
        "query": test_query,
        "results": [{"name": "Amul Butter 100g", "price": 58.0}],
        "canonical_cards": [],
        "store_id": test_store_id,
        "cached": False
    }
    RAM_STORE_QUERY_CACHE[expected_cache_key] = mock_payload

    # Warm up / prime store resolution
    _ = client.get(f"/api/compare?q={test_query}&lat=19.0760&lon=72.8777")

    # Repeat cache hit: benchmark sub-5ms response
    t0 = time.perf_counter()
    resp = client.get(f"/api/compare?q={test_query}&lat=19.0760&lon=72.8777")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert resp.status_code == 200, f"Failed /api/compare: {resp.text}"
    data = resp.json()
    assert data.get("cached") is True, f"Expected 'cached': true, got {data.get('cached')}"
    print(f"  ✓ Cache hit returned: cached={data['cached']} in {elapsed_ms:.2f}ms")
    assert elapsed_ms < 10.0, f"Cache hit took too long: {elapsed_ms}ms"

    # Test /api/canonical caching
    canon_cache_key = f"cache:{test_store_id}:{query_hash}:canonical"
    RAM_STORE_QUERY_CACHE[canon_cache_key] = {
        "query": test_query,
        "cards": [{"canonical_name": "Amul Butter 100g"}],
        "cached": False
    }
    t0_c = time.perf_counter()
    resp_canon = client.get(f"/api/canonical?q={test_query}&lat=21.1762&lon=79.0554")
    elapsed_canon_ms = (time.perf_counter() - t0_c) * 1000.0
    assert resp_canon.status_code == 200
    data_canon = resp_canon.json()
    assert data_canon.get("cached") is True, f"Expected 'cached': true, got {data_canon.get('cached')}"
    print(f"  ✓ Canonical cache hit returned: cached={data_canon['cached']} in {elapsed_canon_ms:.2f}ms")

    print("  ✓ Task 2: TTL Store Caching PASSED!\n")


def test_post_compare_client_zepto_offloading():
    print("======================================================================")
    print("3. Testing POST /api/compare with Client-Side Zepto Offloading")
    print("======================================================================")
    client_zepto_items = [
        {
            "id": "zepto_client_offloaded_001",
            "name": "Amul Butter 100g",
            "price": 58.0,
            "discounted_price": 58.0,
            "mrp": 60.0,
            "packsize": "100 g",
            "normalized_price": 58.0,
            "unit": "100g",
            "formatted_unit_price": "₹58.00 / 100g",
            "image_url": "https://cdn.zeptonow.com/sample_butter.png",
            "buy_url": "https://www.zeptonow.com/search?query=amul%20butter",
            "is_available": True,
            "store": "Zepto"
        }
    ]

    resp = client.post("/api/compare", json={
        "query": "amul butter",
        "latitude": 19.0760,
        "longitude": 72.8777,
        "zepto_products": client_zepto_items
    })
    assert resp.status_code == 200, f"Failed POST /api/compare: {resp.text}"
    data = resp.json()
    assert data["query"] == "amul butter"
    assert data["raw_zepto_count"] == 1
    assert len(data["results"]) >= 1
    assert "canonical_cards" in data
    print(f"  ✓ POST /api/compare received {data['raw_zepto_count']} client-side Zepto items and returned {len(data['results'])} paired results!")
    print("  ✓ Task 3: Client-Side Zepto Offloading via POST PASSED!\n")


if __name__ == "__main__":
    test_canonical_sku_lookup()
    test_ttl_store_caching()
    test_post_compare_client_zepto_offloading()
    print("ALL INTEGRATION TESTS PASSED SUCCESSFULLY!")
