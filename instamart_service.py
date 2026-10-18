"""
instamart_service.py

QuickCompare Swiggy Instamart provider scraper.

Design goals:
- Persistent Chromium session, like the Blinkit/Zepto services.
- User supplied latitude/longitude is the source of location truth.
- Real Instamart location flow is used; no hard-coded store ID.
- Instamart search is executed inside the real browser session.
- First search response is returned through a streaming callback.
- Additional offsets are collected in the background.
- Product parsing is based on the verified current Instamart response shape:
    data.cards[*].card.card.gridElements.infoWithStyle.items[*].variations[*]
- One normalized product is emitted per Instamart SKU/variation.

Standalone only for now. Do not wire into server.py until this file passes
its standalone tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logger = logging.getLogger("instamart_service")

HOME_URL = "https://www.swiggy.com/instamart"
SEARCH_ENDPOINT = "https://www.swiggy.com/api/instamart/search/v2"
LAYOUT_ID = "4987"
PAGE_SIZE = 40
BROWSER_TIMEOUT_MS = 60_000
SEARCH_RESPONSE_TIMEOUT_MS = 12_000

ProductsCallback = Callable[[List[Dict[str, Any]]], Awaitable[None] | None]
ServiceabilityCallback = Callable[[str], Awaitable[None] | None]

# One persistent browser/session for the provider. Searches are serialized
# because one page cannot safely perform two searches at the same time.
_LOCK = asyncio.Lock()

_pw = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None

_CURRENT_LAT: Optional[float] = None
_CURRENT_LON: Optional[float] = None
_CURRENT_STORE_ID: Optional[str] = None
_CURRENT_ADDRESS: str = ""


def _ms() -> float:
    return time.perf_counter() * 1000.0


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and str(value).strip() != "":
            return value
    return None


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    text = re.sub(r"[₹$€£]", "", text)
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _money(value: Any) -> Optional[float]:
    """Parse Instamart money values without guessing normal rupee prices."""
    if isinstance(value, dict):
        units = _as_float(value.get("units"))
        nanos = _as_float(value.get("nanos")) or 0.0
        if units is not None:
            return units + nanos / 1_000_000_000.0
        return None
    return _as_float(value)


def _normalize_image(image_id: Any) -> str:
    if not image_id:
        return ""
    text = str(image_id).strip()
    if text.startswith("http://") or text.startswith("https://"):
        return text
    # Instamart currently exposes catalog image IDs rather than complete URLs.
    # This is the public CDN pattern used by the current web response.
    return "https://media-assets.swiggy.com/swiggy/image/upload/" + text


def _unit_price_from_text(value: Any) -> Optional[float]:
    if not value:
        return None
    text = str(value).strip().lower()
    match = re.search(
        r"(?:₹\s*)?(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*(kg|g|l|ml|unit|piece|pcs)",
        text,
    )
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _extract_unit(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip().lower()
    match = re.search(
        r"/\s*(\d+(?:\.\d+)?)\s*(kg|g|l|ml|unit|piece|pcs)\b",
        text,
    )
    if not match:
        return ""
    return f"{match.group(1)} {match.group(2)}"


def _safe_callback_result(result: Any) -> Awaitable[Any] | None:
    if asyncio.iscoroutine(result):
        return result
    return None


# ---------------------------------------------------------------------------
# Exact Instamart response parser
# ---------------------------------------------------------------------------


def _iter_instamart_variations(body: Any):
    """Yield (card item, variation) from the verified search response shape."""
    if not isinstance(body, dict):
        return

    data = body.get("data")
    if not isinstance(data, dict):
        return

    cards = data.get("cards")
    if not isinstance(cards, list):
        return

    for card_wrapper in cards:
        if not isinstance(card_wrapper, dict):
            continue
        card = card_wrapper.get("card")
        if not isinstance(card, dict):
            continue
        inner = card.get("card")
        if not isinstance(inner, dict):
            continue
        grid = inner.get("gridElements")
        if not isinstance(grid, dict):
            continue
        info = grid.get("infoWithStyle")
        if not isinstance(info, dict):
            continue
        items = info.get("items")
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            variations = item.get("variations")
            if not isinstance(variations, list):
                continue
            for variation in variations:
                if isinstance(variation, dict):
                    yield item, variation


def _normalize_variation(
    item: Dict[str, Any],
    variation: Dict[str, Any],
    store_id: str,
) -> Optional[Dict[str, Any]]:
    name = str(_first(
        variation.get("displayName"),
        item.get("displayName"),
    ) or "").strip()
    brand = str(_first(
        variation.get("brandName"),
        item.get("brand"),
    ) or "").strip()

    price_block = variation.get("price")
    if not isinstance(price_block, dict):
        return None

    offer_price = _money(price_block.get("offerPrice"))
    mrp = _money(price_block.get("mrp"))
    sale_price = _money(price_block.get("salePrice"))
    price = offer_price if offer_price is not None else sale_price
    if price is None:
        price = mrp
    if not name or price is None or price <= 0:
        return None

    sku_id = str(_first(
        variation.get("skuId"),
        variation.get("spinId"),
        "",
    ) or "").strip()
    parent_product_id = str(_first(
        item.get("productId"),
        item.get("parentProductId"),
        variation.get("spinId"),
        "",
    ) or "").strip()
    if not sku_id:
        sku_id = parent_product_id
    if not sku_id:
        return None

    quantity = str(_first(
        variation.get("quantityDescription"),
        variation.get("secondaryQuantityDescription"),
        "",
    ) or "").strip()

    image_ids = variation.get("imageIds")
    image_url = ""
    if isinstance(image_ids, list) and image_ids:
        image_url = _normalize_image(image_ids[0])

    medias = variation.get("medias")
    if not image_url and isinstance(medias, list):
        for media in medias:
            if isinstance(media, dict) and media.get("type") == "MEDIA_TYPE_IMAGE":
                image_url = _normalize_image(media.get("id"))
                if image_url:
                    break

    in_stock = bool(_first(
        variation.get("inventory", {}).get("inStock")
            if isinstance(variation.get("inventory"), dict) else None,
        variation.get("slotInfo", {}).get("isAvail")
            if isinstance(variation.get("slotInfo"), dict) else None,
        item.get("inStock"),
        item.get("isAvail"),
        True,
    ))

    unit_level_price = str(price_block.get("unitLevelPrice") or "").strip()
    normalized_unit_price = _unit_price_from_text(unit_level_price)
    normalized_unit = _extract_unit(unit_level_price)

    # Instamart does not expose a product-detail URL in the verified search
    # object. Keep the provider homepage as a safe navigational fallback.
    buy_url = HOME_URL

    return {
        "id": sku_id,
        "product_id": sku_id,
        "variant_id": sku_id,
        "parent_product_id": parent_product_id,
        "name": name,
        "raw_name": name,
        "brand": brand,
        "price": float(price),
        "discounted_price": float(price),
        "discountedSellingPrice": float(price),
        "selling_price": float(price),
        "mrp": float(mrp if mrp is not None else price),
        "packsize": quantity,
        "image_url": image_url,
        "buy_url": buy_url,
        "store": "Instamart",
        "store_id": str(store_id),
        "is_available": in_stock,
        "out_of_stock": not in_stock,
        "unit_level_price": unit_level_price,
        "normalized_price": normalized_unit_price,
        "unit": normalized_unit,
        "formatted_unit_price": unit_level_price,
        "category": str(variation.get("category") or "").strip(),
        "weight_in_grams": variation.get("weightInGrams"),
        "source": "instamart_search_v2",
    }


def _extract_products(body: Any, store_id: str) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for item, variation in _iter_instamart_variations(body):
        product = _normalize_variation(item, variation, store_id)
        if not product:
            continue
        key = str(product.get("product_id") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        products.append(product)

    return products


# ---------------------------------------------------------------------------
# Persistent browser / location
# ---------------------------------------------------------------------------


async def _ensure_browser(lat: float, lon: float) -> Page:
    global _pw, _browser, _context, _page

    # The geolocation init script is bound to the browser context. If a later
    # QuickCompare search uses a different location, start a fresh context so
    # the browser cannot accidentally retain the previous coordinates.
    if _page is not None and not _page.is_closed():
        if (
            _CURRENT_LAT is None
            or _CURRENT_LON is None
            or abs(_CURRENT_LAT - float(lat)) > 0.00001
            or abs(_CURRENT_LON - float(lon)) > 0.00001
        ):
            await close_instamart_browser()
        else:
            return _page

    if _page is not None and not _page.is_closed():
        return _page

    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
            "--lang=en-IN",
        ],
    )
    _context = await _browser.new_context(
        viewport={"width": 1440, "height": 900},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        geolocation={"latitude": lat, "longitude": lon},
        permissions=["geolocation"],
    )
    _page = await _context.new_page()

    # Use the same deterministic geolocation override that was proven in the
    # V7 Instamart probe. Object.defineProperty is important here because
    # navigator.geolocation is exposed as a browser-provided read-only property
    # in Chromium; simple assignment can silently fail.
    await _page.add_init_script(
        """(coords => {
            const position = {
                coords: {
                    latitude: coords.lat,
                    longitude: coords.lon,
                    accuracy: 10,
                    altitude: null,
                    altitudeAccuracy: null,
                    heading: null,
                    speed: null
                },
                timestamp: Date.now()
            };

            Object.defineProperty(navigator, 'geolocation', {
                configurable: true,
                value: {
                    getCurrentPosition(success, error) {
                        setTimeout(() => success(position), 0);
                    },
                    watchPosition(success, error) {
                        setTimeout(() => success(position), 0);
                        return 1;
                    },
                    clearWatch() {}
                }
            });
        })(%s);"""
        % json.dumps({"lat": float(lat), "lon": float(lon)})
    )
    return _page


async def _get_browser_coords(page: Page) -> Optional[Tuple[float, float]]:
    # Chromium can briefly expose the real geolocation object while a page is
    # transitioning. Retry exactly as the proven V7 probe does instead of
    # treating the first null result as a hard failure.
    for _ in range(5):
        try:
            value = await page.evaluate(
                """
                () => new Promise(resolve => {
                    navigator.geolocation.getCurrentPosition(
                        p => resolve({
                            latitude: p.coords.latitude,
                            longitude: p.coords.longitude,
                            accuracy: p.coords.accuracy
                        }),
                        e => resolve({error: e.code, message: e.message}),
                        {enableHighAccuracy: true, timeout: 5000, maximumAge: 0}
                    );
                })
                """
            )
            if (
                isinstance(value, dict)
                and isinstance(value.get("latitude"), (int, float))
                and isinstance(value.get("longitude"), (int, float))
            ):
                return float(value["latitude"]), float(value["longitude"])
        except Exception:
            pass
        await page.wait_for_timeout(500)
    return None


def _store_id_from_instamart_url(url: str) -> Optional[str]:
    """Extract the store context only from Instamart's verified home route."""
    if "/api/instamart/home/v2" not in str(url or ""):
        return None
    match = re.search(r"[?&]storeId=([^&]+)", str(url))
    return match.group(1) if match else None


async def _discover_store_id(
    page: Page,
    after_ms: float,
    observed_urls: Optional[List[str]] = None,
    allow_timing_fallback: bool = True,
) -> Optional[str]:
    """Use post-location home requests, never the initial default store.

    ``performance`` is only a fallback.  It can omit an in-flight or cached
    resource even though Playwright observed the real location-triggered
    response, which previously made a successful location flow look failed.
    """
    for url in reversed(observed_urls or []):
        store_id = _store_id_from_instamart_url(url)
        if store_id:
            return store_id

    if not allow_timing_fallback:
        return None

    try:
        entries = await page.evaluate(
            """
            () => performance.getEntriesByType('resource').map(e => ({
                name: e.name,
                startTime: e.startTime
            }))
            """
        )
    except Exception:
        return None

    candidates: List[Tuple[float, str]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("name") or "")
        store_id = _store_id_from_instamart_url(url)
        if not store_id:
            continue
        try:
            start = float(entry.get("startTime") or 0.0)
        except Exception:
            start = 0.0
        # after_ms is wall-clock milliseconds, so use the resource's ordering
        # rather than attempting to compare browser and Python clocks.
        candidates.append((start, store_id))

    if not candidates:
        return None

    # The latest home request is the post-location store in the verified flow.
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


async def _select_location(
    page: Page,
    latitude: float,
    longitude: float,
    on_serviceability: Optional[ServiceabilityCallback] = None,
) -> str:
    global _CURRENT_LAT, _CURRENT_LON, _CURRENT_STORE_ID, _CURRENT_ADDRESS

    started = _ms()
    observed_home_urls: List[str] = []
    location_action_started = False

    async def observe_location_response(response) -> None:
        store_id = _store_id_from_instamart_url(response.url)
        if location_action_started and store_id:
            observed_home_urls.append(response.url)

    # Register before navigation so a fast post-location home response cannot
    # race the location code below.  This is observation only; it does not
    # fabricate a store ID or alter the page's location flow.
    page.on("response", observe_location_response)
    # Do NOT put lat/lng in the Instamart URL. The verified V7 probe used the
    # normal user-facing location flow: open Instamart, then click
    # "Use current location". Passing coordinates in the URL can cause the
    # frontend to initialize a different location path before geolocation is
    # requested.
    try:
        await page.goto(
            HOME_URL,
            wait_until="domcontentloaded",
            timeout=BROWSER_TIMEOUT_MS,
        )
        await page.wait_for_timeout(4000)

        # Instamart currently exposes the startup location CTA directly.
        candidates = page.get_by_text("Use current location", exact=True)
        clicked = False
        for i in range(await candidates.count()):
            try:
                loc = candidates.nth(i)
                if await loc.is_visible():
                    location_action_started = True
                    await loc.click(timeout=5000)
                    clicked = True
                    break
            except Exception:
                continue

        # Some sessions already have an address. In that case the CTA may not be
        # present. We still verify the browser coordinates before proceeding.
        coords = await _get_browser_coords(page)
        if coords is None:
            try:
                current_url = page.url
            except Exception:
                current_url = "<unknown>"
            raise RuntimeError(
                "Instamart browser geolocation could not be verified "
                f"(url={current_url})"
            )

        if abs(coords[0] - latitude) > 0.001 or abs(coords[1] - longitude) > 0.001:
            raise RuntimeError(
                f"Instamart browser coordinates mismatch: requested={latitude},{longitude} "
                f"browser={coords[0]},{coords[1]}"
            )

        if clicked:
            # Wait for an observable location request rather than assuming a
            # fixed delay makes the store context available.
            deadline = asyncio.get_running_loop().time() + 8.0
            while not observed_home_urls and asyncio.get_running_loop().time() < deadline:
                await page.wait_for_timeout(200)

        store_id = await _discover_store_id(
            page,
            started,
            observed_home_urls,
            allow_timing_fallback=not clicked,
        )
        if not store_id:
            raise RuntimeError("Instamart did not expose a post-location storeId")

        # Verify the address bar after the location operation. This prevents a
        # stale/default session from being treated as valid.
        address_text = ""
        try:
            address = page.locator('[data-testid="address-bar"]').first
            if await address.count() and await address.is_visible():
                address_text = (await address.inner_text()).strip()
        except Exception:
            pass

        if clicked and not address_text:
            # Location click happened, but Instamart did not visibly acknowledge
            # the selected address. Do not silently continue with a stale store.
            raise RuntimeError("Instamart location was clicked but address bar was not confirmed")

        _CURRENT_LAT = latitude
        _CURRENT_LON = longitude
        _CURRENT_STORE_ID = str(store_id)
        _CURRENT_ADDRESS = address_text

        if on_serviceability:
            result = on_serviceability("serviceable")
            awaited = _safe_callback_result(result)
            if awaited:
                await awaited

        logger.info(
            "Instamart location ready | store=%s | address=%s | elapsed=%.0fms",
            store_id,
            address_text,
            _ms() - started,
        )
        return str(store_id)
    finally:
        page.remove_listener("response", observe_location_response)


async def _ensure_location(
    latitude: float,
    longitude: float,
    on_serviceability: Optional[ServiceabilityCallback] = None,
) -> Tuple[Page, str]:
    global _CURRENT_LAT, _CURRENT_LON, _CURRENT_STORE_ID

    async with _LOCK:
        page = await _ensure_browser(latitude, longitude)
        same = (
            _CURRENT_LAT is not None
            and _CURRENT_LON is not None
            and abs(_CURRENT_LAT - latitude) < 0.00001
            and abs(_CURRENT_LON - longitude) < 0.00001
            and _CURRENT_STORE_ID
        )
        if same:
            return page, str(_CURRENT_STORE_ID)
        store_id = await _select_location(
            page,
            latitude,
            longitude,
            on_serviceability=on_serviceability,
        )
        return page, store_id


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def _browser_search_request(
    page: Page,
    query: str,
    store_id: str,
    offset: int,
) -> Dict[str, Any]:
    payload = {
        "facets": [],
        "sortAttribute": "",
        "query": query,
        "search_results_offset": str(offset),
        "page_type": "INSTAMART_SEARCH_PAGE",
        "is_pre_search_tag": False,
    }

    url = (
        f"{SEARCH_ENDPOINT}?offset={offset}"
        f"&ageConsent=false"
        f"&layoutId={LAYOUT_ID}"
        f"&voiceSearchTrackingId="
        f"&storeId={quote(str(store_id), safe='')}"
        f"&primaryStoreId={quote(str(store_id), safe='')}"
        f"&secondaryStoreId="
    )

    started = _ms()
    result = await page.evaluate(
        """
        async ({url, payload}) => {
          const response = await fetch(url, {
            method: 'POST',
            credentials: 'include',
            headers: {
              'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
          });
          let body = null;
          try { body = await response.json(); }
          catch (_) { body = await response.text(); }
          return {
            status: response.status,
            body
          };
        }
        """,
        {"url": url, "payload": payload},
    )
    elapsed = _ms() - started

    if not isinstance(result, dict):
        raise RuntimeError("Instamart search returned an invalid browser result")
    if int(result.get("status") or 0) != 200:
        raise RuntimeError(
            f"Instamart search failed: status={result.get('status')} offset={offset}"
        )

    return {
        "query": query,
        "offset": offset,
        "page_type": "INSTAMART_SEARCH_PAGE",
        "url": url,
        "payload": payload,
        "status": result.get("status"),
        "elapsed_ms": round(elapsed, 1),
        "body": result.get("body"),
    }


async def fetch_instamart_products_streaming(
    query: str,
    latitude: float,
    longitude: float,
    on_first_batch: Optional[ProductsCallback] = None,
    on_background_batch: Optional[ProductsCallback] = None,
    on_serviceability: Optional[ServiceabilityCallback] = None,
    max_retries: int = 3,
    max_pages: int = 8,
    page_size: int = PAGE_SIZE,
) -> List[Dict[str, Any]]:
    """
    Fetch Instamart products progressively.

    First successful offset is sent to on_first_batch immediately. Subsequent
    offsets are fetched in the same persistent browser session and sent to
    on_background_batch. The returned list contains all products collected by
    the time the function completes.
    """
    del page_size  # Instamart's verified frontend offset is 40.

    query = str(query or "").strip()
    if not query:
        raise ValueError("Instamart query cannot be empty")
    if not (-90 <= float(latitude) <= 90 and -180 <= float(longitude) <= 180):
        raise ValueError("Invalid latitude/longitude")

    async with _LOCK:
        page = await _ensure_browser(float(latitude), float(longitude))
        store_id = await _select_location(
            page,
            float(latitude),
            float(longitude),
            on_serviceability=on_serviceability,
        ) if not (
            _CURRENT_STORE_ID
            and _CURRENT_LAT is not None
            and _CURRENT_LON is not None
            and abs(_CURRENT_LAT - float(latitude)) < 0.00001
            and abs(_CURRENT_LON - float(longitude)) < 0.00001
        ) else str(_CURRENT_STORE_ID)

        all_products: List[Dict[str, Any]] = []
        seen: set[str] = set()
        first_sent = False

        for page_index in range(max(1, int(max_pages))):
            offset = page_index * PAGE_SIZE
            last_error: Optional[Exception] = None
            response = None

            for attempt in range(max(1, int(max_retries))):
                try:
                    response = await asyncio.wait_for(
                        _browser_search_request(page, query, store_id, offset),
                        timeout=SEARCH_RESPONSE_TIMEOUT_MS / 1000.0,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < max(1, int(max_retries)):
                        await page.wait_for_timeout(300 * (attempt + 1))

            if response is None:
                if first_sent:
                    logger.warning(
                        "Instamart background offset failed | query=%s offset=%s error=%s",
                        query, offset, last_error,
                    )
                    break
                raise RuntimeError(
                    f"Instamart first search failed after retries: {last_error}"
                )

            products = _extract_products(response.get("body"), store_id)
            new_products: List[Dict[str, Any]] = []
            for product in products:
                key = str(product.get("product_id") or product.get("id") or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                all_products.append(product)
                new_products.append(product)

            if not first_sent:
                first_sent = True
                if on_first_batch:
                    result = on_first_batch(new_products)
                    awaited = _safe_callback_result(result)
                    if awaited:
                        await awaited
            elif new_products and on_background_batch:
                result = on_background_batch(new_products)
                awaited = _safe_callback_result(result)
                if awaited:
                    await awaited

            logger.info(
                "Instamart search | query=%s offset=%d raw=%d new=%d elapsed=%.1fms",
                query,
                offset,
                len(products),
                len(new_products),
                float(response.get("elapsed_ms") or 0),
            )

            # A short/empty page means there is no more result set.
            if not products or len(products) < PAGE_SIZE:
                break

        return all_products


async def fetch_instamart_products(
    query: str,
    latitude: float,
    longitude: float,
    max_pages: int = 8,
    page_size: int = PAGE_SIZE,
) -> List[Dict[str, Any]]:
    """Compatibility wrapper matching the full-return provider contract."""
    return await fetch_instamart_products_streaming(
        query=query,
        latitude=latitude,
        longitude=longitude,
        max_pages=max_pages,
        page_size=page_size,
    )


fetch_products = fetch_instamart_products


async def close_instamart_browser() -> None:
    global _pw, _browser, _context, _page
    global _CURRENT_LAT, _CURRENT_LON, _CURRENT_STORE_ID, _CURRENT_ADDRESS

    try:
        if _context is not None:
            await _context.close()
    finally:
        _context = None
        _page = None
        try:
            if _browser is not None:
                await _browser.close()
        finally:
            _browser = None
            if _pw is not None:
                await _pw.stop()
            _pw = None
            _CURRENT_LAT = None
            _CURRENT_LON = None
            _CURRENT_STORE_ID = None
            _CURRENT_ADDRESS = ""


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Standalone Instamart scraper test")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--query", type=str, required=True)
    parser.add_argument("--max-pages", type=int, default=2)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    first_count = 0
    background_count = 0

    async def first(items: List[Dict[str, Any]]) -> None:
        nonlocal first_count
        first_count = len(items)
        print()
        print("=" * 80)
        print("INSTAMART FIRST BATCH")
        print("=" * 80)
        print("Products:", len(items))
        for i, p in enumerate(items[:10], 1):
            print(
                f"{i}. {p['name']} | price={p['price']} | mrp={p['mrp']} "
                f"| pack={p['packsize']} | sku={p['product_id']}"
            )

    async def background(items: List[Dict[str, Any]]) -> None:
        nonlocal background_count
        background_count += len(items)
        print(f"BACKGROUND BATCH: +{len(items)} products")

    async def serviceability(state: str) -> None:
        print("SERVICEABILITY:", state)

    try:
        print("=" * 80)
        print("INSTAMART QUICKCOMPARE STANDALONE TEST")
        print("=" * 80)
        print(f"Coordinates : {args.lat}, {args.lon}")
        print(f"Query       : {args.query}")
        print(f"Max pages   : {args.max_pages}")
        print("Browser     : headed persistent Chromium")

        started = _ms()
        products = await fetch_instamart_products_streaming(
            args.query,
            args.lat,
            args.lon,
            on_first_batch=first,
            on_background_batch=background,
            on_serviceability=serviceability,
            max_pages=args.max_pages,
        )

        print()
        print("=" * 80)
        print("TEST COMPLETE")
        print("=" * 80)
        print("Total products:", len(products))
        print("First batch   :", first_count)
        print("Background    :", background_count)
        print("Store ID      :", _CURRENT_STORE_ID)
        print("Address       :", _CURRENT_ADDRESS)
        print("Elapsed       : %.1f ms" % (_ms() - started))
        print()
        print("First 20 normalized products:")
        for i, p in enumerate(products[:20], 1):
            print(
                f"{i:02d}. {p['name']} | {p['price']} | MRP {p['mrp']} "
                f"| {p['packsize']} | {p['product_id']}"
            )
    finally:
        await close_instamart_browser()


if __name__ == "__main__":
    asyncio.run(_main())
