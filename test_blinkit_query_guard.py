from types import SimpleNamespace

from blinkit_service import _blinkit_request_matches_query


def test_blinkit_response_guard_accepts_current_query():
    request = SimpleNamespace(post_data='{"query": "milk"}')
    assert _blinkit_request_matches_query(request, "Milk")


def test_blinkit_response_guard_rejects_stale_query():
    request = SimpleNamespace(post_data='{"query": "bread"}')
    assert not _blinkit_request_matches_query(request, "milk")


def test_blinkit_response_guard_handles_form_and_missing_payload():
    assert _blinkit_request_matches_query(
        SimpleNamespace(post_data="q=milk"), "milk"
    )
    assert _blinkit_request_matches_query(SimpleNamespace(post_data=""), "milk")
