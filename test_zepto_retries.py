import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import zepto_service as zepto


class ZeptoRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = [
            patch.object(zepto, "_ZEPTO_PERSISTENT_LOCK", asyncio.Lock()),
            patch.object(zepto, "SEARCH_RETRY_DELAY_SECONDS", 0),
            patch.object(zepto, "_get_persistent_zepto_session", AsyncMock(return_value=(None, None, object(), object(), "store"))),
            patch.object(zepto, "_close_persistent_zepto_session", AsyncMock()),
            patch.object(zepto, "get_zepto_serviceability_state", return_value={"status": "serviceable"}),
            patch.object(zepto, "_perform_browser_search", AsyncMock()),
            patch.object(zepto, "extract_products", return_value=[{"id": "one"}]),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def fetch(self):
        return await zepto.fetch_zepto_products("milk", 21.1, 79.0)

    async def test_timeout_retries_with_fresh_session(self):
        zepto._perform_browser_search.side_effect = [zepto.PlaywrightTimeoutError("timeout"), {}]
        self.assertEqual(await self.fetch(), [{"id": "one"}])
        self.assertEqual(zepto._get_persistent_zepto_session.await_count, 2)
        zepto._close_persistent_zepto_session.assert_awaited_once()

    async def test_exhaustion_raises_instead_of_empty_success(self):
        zepto._perform_browser_search.side_effect = RuntimeError("no response")
        with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
            await self.fetch()
        self.assertEqual(zepto._perform_browser_search.await_count, 3)
        self.assertEqual(zepto._close_persistent_zepto_session.await_count, 3)

    async def test_unserviceable_does_not_retry(self):
        zepto.get_zepto_serviceability_state.return_value = {"status": "unserviceable"}
        self.assertEqual(await self.fetch(), [])
        zepto._perform_browser_search.assert_not_awaited()

    async def test_valid_empty_results_do_not_retry(self):
        zepto.extract_products.return_value = []
        self.assertEqual(await self.fetch(), [])
        zepto._perform_browser_search.assert_awaited_once()

    async def test_unknown_empty_results_retry(self):
        zepto.get_zepto_serviceability_state.return_value = {"status": "unknown"}
        zepto.extract_products.return_value = []
        with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
            await self.fetch()
        self.assertEqual(zepto._perform_browser_search.await_count, 3)

    async def test_cancellation_propagates_without_retry(self):
        zepto._perform_browser_search.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.fetch()
        zepto._perform_browser_search.assert_awaited_once()
        zepto._close_persistent_zepto_session.assert_awaited_once()
