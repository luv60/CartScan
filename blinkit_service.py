
"""
blinkit_service.py - Persistent browser Blinkit service

Fast path:
- One persistent Chromium session.
- Blinkit location is established once per process/requested coordinate.
- Each search uses the visible Blinkit search UI.
- Captures only the first /v1/layout/search response.
- Does not scroll or paginate before returning.
- Preserves the existing QuickCompare product output schema.

The old direct HTTP/pagination behavior is retained as an optional fallback
function so it can be restored/tested separately if needed.
"""

import asyncio
import json
import logging
import random
import re
import urllib.parse
from typing import List, Dict, Any, Optional, Tuple

from curl_cffi.requests import AsyncSession
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Browser,
    BrowserContext,
    Page,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("blinkit_service")

app = FastAPI(
    title="Blinkit Persistent Browser Scraper Service",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BLINKIT_HOME = "https://blinkit.com/"
BLINKIT_SEARCH_PATH = "/v1/layout/search"

DEFAULT_LAT = 21.1718692
DEFAULT_LON = 79.0506339

BROWSER_TIMEOUT_MS = 30_000
LOCATION_TIMEOUT_MS = 15_000
SEARCH_TIMEOUT_MS = 10_000

# One browser/context/page is deliberately shared. Searches are serialized
# because one visible Blinkit page cannot safely perform two searches at once.
_browser_lock = asyncio.Lock()
_search_lock = asyncio.Lock()

_playwright = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None

_active_lat: Optional[float] = None
_active_lon: Optional[float] = None
_location_ready = False
# Serviceability is separate from scraper success.
# serviceable   = Blinkit search endpoint accepted the location.
# unserviceable = Blinkit explicitly returned "location not serviceable".
# unknown       = serviceability could not be established.
_BLINKIT_SERVICEABILITY = "unknown"
_BLINKIT_SERVICEABILITY_MESSAGE = ""


def _blinkit_request_matches_query(request: Any, query: str) -> bool:
    """Return whether a Blinkit search request belongs to ``query``.

    A persistent page can finish a response from the prior search just after a
    new response listener is installed. Capturing that response makes a new
    search appear to return stale or partial products. Blinkit's request
    payload normally carries the typed query; when it is unavailable we leave
    the response eligible rather than rejecting a valid search solely because
    Playwright did not expose its body.
    """
    expected = " ".join((query or "").casefold().split())
    if not expected:
        return False

    try:
        raw_payload = request.post_data or ""
    except Exception:
        return True

    if not raw_payload:
        return True

    decoded_payload = urllib.parse.unquote_plus(str(raw_payload))
    try:
        payload = json.loads(decoded_payload)
    except (TypeError, ValueError):
        return expected in " ".join(decoded_payload.casefold().split())

    query_keys = {
        "query", "q", "search", "search_query", "searchquery",
        "search_term", "searchterm", "term", "keyword",
    }

    def contains_query(value: Any, key_hint: str = "") -> bool:
        if isinstance(value, dict):
            return any(contains_query(v, str(k)) for k, v in value.items())
        if isinstance(value, list):
            return any(contains_query(v, key_hint) for v in value)
        if key_hint.casefold().replace("-", "_") not in query_keys:
            return False
        return " ".join(str(value).casefold().split()) == expected

    return contains_query(payload)


# ---------------------------------------------------------------------------
# Existing normalization helpers
# ---------------------------------------------------------------------------

def extract_quantity_in_base_units(text: str):
    if not text:
        return 1.0, "unit"

    t = text.lower()

    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:l|ltr|litre|litres|liter)\b",
        t,
    )
    if m:
        return float(m.group(1)) * 1000.0, "ml"

    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:ml|milli)\b",
        t,
    )
    if m:
        return float(m.group(1)), "ml"

    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:kg|kilo|kilogram)\b",
        t,
    )
    if m:
        return float(m.group(1)) * 1000.0, "g"

    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:g|gm|grams?)\b",
        t,
    )
    if m:
        return float(m.group(1)), "g"

    return 1.0, "unit"


def extract_packsize_from_title(title: str, current_pack: str = "") -> str:
    current = (current_pack or "").strip()
    c_lower = current.lower()

    if not current or c_lower in [
        "1 pack",
        "1 pc",
        "1 piece",
        "pack",
        "unit",
        "1 unit",
        "",
    ]:
        m = re.search(
            r"\b(\d+(?:\.\d+)?)\s*"
            r"(ml|l|ltr|litre|litres|liter|kg|kilo|g|gm|grams?)\b",
            title,
            re.IGNORECASE,
        )
        if m:
            val = m.group(1)
            u = m.group(2).lower()

            if u in ["l", "ltr", "litre", "litres", "liter"]:
                return f"{val} L"
            if u == "ml":
                return f"{val} ml"
            if u in ["kg", "kilo"]:
                return f"{val} kg"
            if u in ["g", "gm", "gram", "grams"]:
                return f"{val} g"

            return f"{val} {u}"

    return current or "1 pack"


def calculate_unit_price(
    name: str,
    packsize: str,
    price: float,
) -> Dict[str, Any]:
    if price is None or price <= 0:
        return {
            "normalized_price": 0.0,
            "unit": "per pack",
            "formatted": "₹0.00 / pack",
        }

    combined = f"{name} {packsize}".lower()

    mult_match = re.search(
        r"(\d+)\s*[xX*]\s*"
        r"(\d+(?:\.\d+)?)\s*"
        r"(kg|kilo|kilogram|g|gm|gms|gram|l|ltr|litre|liter|ml)\b",
        combined,
    )

    if mult_match:
        multiplier = float(mult_match.group(1))
        sub_qty, base_u = extract_quantity_in_base_units(
            f"{mult_match.group(2)} {mult_match.group(3)}"
        )
        total_base = multiplier * sub_qty

        if base_u in ("g", "ml") and total_base > 0:
            norm = round((price / total_base) * 100, 2)
            unit = "100g" if base_u == "g" else "100ml"
            return {
                "normalized_price": norm,
                "unit": unit,
                "formatted": f"₹{norm:.2f} / {unit}",
            }

    base_qty, base_u = extract_quantity_in_base_units(combined)

    if base_u in ("g", "ml") and base_qty > 0:
        norm = round((price / base_qty) * 100, 2)
        unit = "100g" if base_u == "g" else "100ml"
        return {
            "normalized_price": norm,
            "unit": unit,
            "formatted": f"₹{norm:.2f} / {unit}",
        }

    return {
        "normalized_price": round(price, 2),
        "unit": "per pack",
        "formatted": f"₹{price:.2f} / pack",
    }


def _money_value(value: Any) -> float:
    if isinstance(value, dict):
        value = value.get("text", "")

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value or "")
    num = re.sub(r"[^\d.]", "", text)

    if not num:
        return 0.0

    try:
        return float(num)
    except ValueError:
        return 0.0


def _first_image(data: Dict[str, Any]) -> str:
    candidates = [data]

    product = data.get("product")
    if isinstance(product, dict):
        candidates.append(product)

    product_info = data.get("product_info")
    if isinstance(product_info, dict):
        candidates.append(product_info)

    cart_item = data.get("atc_action", {}).get("add_to_cart", {}).get(
        "cart_item", {}
    )
    if isinstance(cart_item, dict):
        candidates.append(cart_item)

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        image_url = candidate.get("image_url")
        if isinstance(image_url, str) and image_url:
            return image_url

        image = candidate.get("image")
        if isinstance(image, dict):
            url = image.get("url") or image.get("image_url")
            if isinstance(url, str) and url:
                return url

        if isinstance(image, str) and image:
            return image

    return ""


def _normalize_image_url(url: str) -> str:
    if not url:
        return ""

    if url.startswith("http"):
        return url

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return "https://cdn.grofers.com" + url

    return "https://cdn.grofers.com/" + url


def extract_blinkit_products_from_first_response(
    body: Any,
    query: str,
) -> List[Dict[str, Any]]:
    """
    Parse Blinkit's actual first-response schema.

    Observed shape:
      response.snippets[*]
        widget_type = product_card_snippet_type_2
        data.name.text
        data.normal_price.text
        data.mrp.text
        data.variant.text
        data.product_id
        data.inventory
    """
    if not isinstance(body, dict):
        return []

    response = body.get("response")
    if not isinstance(response, dict):
        return []

    snippets = response.get("snippets")
    if not isinstance(snippets, list):
        return []

    products: List[Dict[str, Any]] = []
    seen_keys = set()

    for snippet in snippets:
        if not isinstance(snippet, dict):
            continue

        widget_type = str(snippet.get("widget_type", "")).lower()
        # Blinkit has more than one product-card widget name. Keep this
        # structural check broad enough for its promoted/starting cards while
        # excluding non-product widgets such as banners and headings.
        if "product" not in widget_type or "card" not in widget_type:
            continue

        sd = snippet.get("data")
        if not isinstance(sd, dict):
            continue

        product = sd.get("product")
        if not isinstance(product, dict):
            product = {}
        product_info = sd.get("product_info")
        if not isinstance(product_info, dict):
            product_info = {}

        cart_item = (
            sd.get("atc_action", {})
            .get("add_to_cart", {})
            .get("cart_item", {})
        )
        if not isinstance(cart_item, dict):
            cart_item = {}

        name_obj = (
            sd.get("name")
            or sd.get("display_name")
            or product.get("name")
            or product.get("display_name")
            or product.get("title")
            or product_info.get("name")
            or product_info.get("display_name")
            or product_info.get("title")
        )
        if isinstance(name_obj, dict):
            name = name_obj.get("text", "")
        else:
            name = str(name_obj or "")

        if not name:
            name = (
                cart_item.get("product_name")
                or cart_item.get("display_name")
                or ""
            )

        name = str(name).strip()

        product_id = (
            sd.get("product_id")
            or sd.get("variant_id")
            or sd.get("id")
            or product.get("product_id")
            or product.get("variant_id")
            or product.get("id")
            or product_info.get("product_id")
            or product_info.get("variant_id")
            or product_info.get("id")
            or cart_item.get("product_id")
            or cart_item.get("id")
        )

        if product_id is None:
            identity = sd.get("identity")
            if isinstance(identity, dict):
                product_id = identity.get("id")

        if not name or product_id is None:
            continue

        key = str(product_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        price = _money_value(
            cart_item.get("price")
            or sd.get("normal_price")
            or sd.get("price")
            or sd.get("selling_price")
            or sd.get("discounted_selling_price")
            or product.get("price")
            or product.get("selling_price")
            or product.get("discounted_selling_price")
            or product_info.get("price")
            or product_info.get("selling_price")
            or product_info.get("discounted_selling_price")
        )

        mrp = _money_value(
            cart_item.get("mrp")
            or sd.get("mrp")
            or sd.get("strike_through_price")
            or product.get("mrp")
            or product_info.get("mrp")
        )

        if mrp <= 0 and price > 0:
            mrp = price

        unit_obj = (
            sd.get("variant")
            or product.get("variant")
            or product_info.get("variant")
            or product.get("pack_size")
            or product_info.get("pack_size")
        )
        if isinstance(unit_obj, dict):
            unit = unit_obj.get("text", "")
        else:
            unit = str(unit_obj or "")

        if not unit:
            unit = str(cart_item.get("unit") or "")

        inventory = (
            cart_item.get("inventory")
            if "inventory" in cart_item
            else sd.get("inventory")
        )

        out_of_stock = False

        if isinstance(inventory, (int, float)):
            out_of_stock = inventory <= 0
        elif isinstance(inventory, dict):
            out_of_stock = (
                inventory.get("is_out_of_stock") is True
                or inventory.get("quantity", 1) <= 0
            )

        if sd.get("is_out_of_stock") is True:
            out_of_stock = True

        if cart_item.get("is_out_of_stock") is True:
            out_of_stock = True

        if "notify" in str(sd.get("atc_action", {})).lower():
            out_of_stock = True

        clean_pack = extract_packsize_from_title(
            name,
            unit or "1 pack",
        )

        unit_calc = calculate_unit_price(
            name,
            clean_pack,
            price,
        )

        image_url = _normalize_image_url(_first_image(sd))

        encoded_q = urllib.parse.quote(query)
        buy_url = (
            f"https://blinkit.com/prn/x/prid/{product_id}"
            if product_id
            else f"https://blinkit.com/s/?q={encoded_q}"
        )

        products.append(
            {
                "name": name,
                "raw_name": name,
                "price": price,
                "discounted_selling_price": price,
                "mrp": mrp,
                "out_of_stock": out_of_stock,
                "is_available": not out_of_stock,
                "packsize": clean_pack,
                "normalized_price": unit_calc["normalized_price"],
                "unit": unit_calc["unit"],
                "formatted_unit_price": unit_calc["formatted"],
                "image_url": image_url,
                "buy_url": buy_url,
                "product_id": str(product_id),
                "store": "Blinkit",
            }
        )

    return products


# ---------------------------------------------------------------------------
# Persistent browser session
# ---------------------------------------------------------------------------

async def _close_browser_locked() -> None:
    global _playwright, _browser, _context, _page
    global _active_lat, _active_lon, _location_ready
    global _BLINKIT_SERVICEABILITY, _BLINKIT_SERVICEABILITY_MESSAGE

    try:
        if _context is not None:
            await _context.close()
    except Exception:
        pass

    try:
        if _browser is not None:
            await _browser.close()
    except Exception:
        pass

    try:
        if _playwright is not None:
            await _playwright.stop()
    except Exception:
        pass

    _playwright = None
    _browser = None
    _context = None
    _page = None
    _active_lat = None
    _active_lon = None
    _location_ready = False
    _BLINKIT_SERVICEABILITY = "unknown"
    _BLINKIT_SERVICEABILITY_MESSAGE = ""


async def _start_browser_locked(lat: float, lon: float) -> Page:
    global _playwright, _browser, _context, _page
    global _active_lat, _active_lon, _location_ready

    _playwright = await async_playwright().start()

    _browser = await _playwright.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    )

    _context = await _browser.new_context(
        viewport={"width": 1440, "height": 900},
        geolocation={
            "latitude": lat,
            "longitude": lon,
        },
        permissions=["geolocation"],
        locale="en-IN",
        timezone_id="Asia/Kolkata",
    )

    _page = await _context.new_page()
    _page.set_default_timeout(BROWSER_TIMEOUT_MS)

    _active_lat = lat
    _active_lon = lon
    _location_ready = False

    started = asyncio.get_running_loop().time()

    await _page.goto(
        BLINKIT_HOME,
        wait_until="domcontentloaded",
        timeout=BROWSER_TIMEOUT_MS,
    )

    logger.info(
        "[Blinkit FAST] browser page ready in %.0f ms",
        (asyncio.get_running_loop().time() - started) * 1000,
    )

    await _page.wait_for_timeout(100)

    await _establish_location_locked(_page)

    _location_ready = True

    logger.info(
        "[Blinkit FAST] persistent session ready | lat=%.7f lon=%.7f",
        lat,
        lon,
    )

    return _page


async def _establish_location_locked(page: Page) -> None:
    """
    Handle the fresh-session location modal.

    The browser context already exposes the requested coordinates through the
    geolocation permission. We deliberately use Blinkit's own Detect my
    location button instead of typing coordinates into the address field.
    """
    try:
        detect_button = page.get_by_text(
            "Detect my location",
            exact=True,
        )

        await detect_button.wait_for(
            state="visible",
            timeout=4_000,
        )

        logger.info("[Blinkit FAST] location modal detected")

        await detect_button.click()

        deadline = (
            asyncio.get_running_loop().time()
            + LOCATION_TIMEOUT_MS / 1000.0
        )

        while asyncio.get_running_loop().time() < deadline:
            try:
                if not await detect_button.is_visible():
                    break
            except Exception:
                break

            await page.wait_for_timeout(100)

    except PlaywrightTimeoutError:
        # No modal means Blinkit already has a usable location/session.
        pass

    # The desktop search bar can be either an existing input or a clickable
    # container which mounts the input when clicked.
    input_locator = page.locator(
        'input[placeholder*="Search"], '
        'input[placeholder*="search"], '
        'input[type="search"]'
    ).first

    try:
        await input_locator.wait_for(
            state="visible",
            timeout=2_000,
        )
        return
    except Exception:
        pass

    container = page.locator(
        '[class*="SearchBar__Container"]'
    ).first

    await container.wait_for(
        state="visible",
        timeout=LOCATION_TIMEOUT_MS,
    )

    await container.click()
    await page.wait_for_timeout(50)

    await input_locator.wait_for(
        state="visible",
        timeout=5_000,
    )


async def _ensure_browser(
    latitude: Optional[float],
    longitude: Optional[float],
) -> Page:
    lat = float(
        latitude
        if latitude is not None
        else DEFAULT_LAT
    )
    lon = float(
        longitude
        if longitude is not None
        else DEFAULT_LON
    )

    async with _browser_lock:
        global _page, _location_ready, _active_lat, _active_lon

        if (
            _page is not None
            and not _page.is_closed()
            and _location_ready
            and _active_lat is not None
            and _active_lon is not None
            and abs(_active_lat - lat) < 0.00001
            and abs(_active_lon - lon) < 0.00001
        ):
            return _page

        # A different location needs a fresh browser context because
        # geolocation permission is context-scoped.
        if _page is not None:
            await _close_browser_locked()

        return await _start_browser_locked(lat, lon)


async def _fast_browser_search(
    query: str,
    latitude: Optional[float],
    longitude: Optional[float],
    on_first_batch=None,
    on_serviceability=None,
) -> List[Dict[str, Any]]:
    global _BLINKIT_SERVICEABILITY, _BLINKIT_SERVICEABILITY_MESSAGE
    _BLINKIT_SERVICEABILITY = "unknown"
    _BLINKIT_SERVICEABILITY_MESSAGE = ""
    """
    Known-good Blinkit first-response path.

    IMPORTANT: The actual search/capture sequence is intentionally kept the
    same as the successfully tested fast implementation. The optional
    callback runs only after the first 200 response has been parsed.
    """
    page = await _ensure_browser(latitude, longitude)

    async with _search_lock:
        search_box = page.locator(
            'input[placeholder*="Search"], '
            'input[placeholder*="search"], '
            'input[type="search"]'
        ).first

        try:
            await search_box.wait_for(state="visible", timeout=1_500)
        except Exception:
            container = page.locator('[class*="SearchBar__Container"]').first
            await container.wait_for(state="visible", timeout=BROWSER_TIMEOUT_MS)
            await container.click()
            await page.wait_for_timeout(50)
            await search_box.wait_for(state="visible", timeout=5_000)

        captured: Dict[str, Any] = {"response": None}

        async def on_response(response):
            if captured["response"] is not None:
                return
            if BLINKIT_SEARCH_PATH not in response.url:
                return
            if response.request.method != "POST":
                return
            if not _blinkit_request_matches_query(response.request, query):
                logger.debug(
                    "[Blinkit FAST] ignored stale search response | query=%r | url=%s",
                    query,
                    response.url,
                )
                return
            try:
                body = await response.json()
                captured["response"] = {
                    "status": response.status,
                    "url": response.url,
                    "body": body,
                }
            except Exception:
                pass

        page.on("response", on_response)
        started = asyncio.get_running_loop().time()

        try:
            # This is the exact proven search sequence.
            await search_box.click()
            await search_box.fill(query)
            await search_box.press("Enter")

            deadline = (
                asyncio.get_running_loop().time()
                + SEARCH_TIMEOUT_MS / 1000.0
            )

            while asyncio.get_running_loop().time() < deadline:
                if captured["response"] is not None:
                    break
                await page.wait_for_timeout(20)

            elapsed_ms = (
                asyncio.get_running_loop().time() - started
            ) * 1000.0

            if captured["response"] is None:
                logger.warning(
                    "[Blinkit FAST] search timed out | query=%r | %.0f ms",
                    query,
                    elapsed_ms,
                )
                return []

            status = captured["response"]["status"]
            body = captured["response"]["body"]

            # Blinkit explicitly reports an unserviceable location in the
            # search response. A normal 200 search response establishes that
            # the location is serviceable, even if the query has no products.
            serviceable = None
            if status == 200:
                serviceable = True
            elif isinstance(body, dict):
                error_text = str(body.get("error") or "").strip().lower()
                if "location not serviceable" in error_text:
                    serviceable = False

            if serviceable is not None:
                if serviceable:
                    _BLINKIT_SERVICEABILITY = "serviceable"
                    _BLINKIT_SERVICEABILITY_MESSAGE = "Blinkit is serviceable at this location."
                else:
                    _BLINKIT_SERVICEABILITY = "unserviceable"
                    _BLINKIT_SERVICEABILITY_MESSAGE = "Blinkit is not serviceable at this location."

            if on_serviceability is not None and serviceable is not None:
                result = on_serviceability(serviceable)
                if asyncio.iscoroutine(result):
                    await result
                logger.info(
                    "[Blinkit FAST] EXPLICIT SERVICEABILITY: %s | status=%s",
                    serviceable,
                    status,
                )

            products = extract_blinkit_products_from_first_response(body, query)

            logger.info(
                "[Blinkit FAST] search complete | query=%r | status=%s | "
                "first_response=%.0f ms | products=%d",
                query,
                status,
                elapsed_ms,
                len(products),
            )

            if status != 200:
                logger.warning(
                    "[Blinkit FAST] non-200 response | query=%r | status=%s",
                    query,
                    status,
                )
                return []

            if on_first_batch is not None and products:
                try:
                    result = on_first_batch(list(products))
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    # A job callback must never break Blinkit's own successful
                    # search result.
                    logger.exception(
                        "[Blinkit FAST] first-batch callback failed | query=%r",
                        query,
                    )

            return products

        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass


async def _collect_blinkit_remaining_in_background(
    query: str,
    latitude: float,
    longitude: float,
    first_products: List[Dict[str, Any]],
    on_background_batch=None,
) -> List[Dict[str, Any]]:
    """
    Continue the already-open Blinkit results page AFTER first delivery.

    This deliberately does not touch the proven first-response code. It uses
    Blinkit's normal lazy-loading behavior by scrolling the existing results
    page and capturing subsequent /v1/layout/search responses.
    """
    page = _page
    if page is None or page.is_closed():
        logger.warning("[Blinkit FAST] background collection skipped: page unavailable")
        return list(first_products)

    all_products = list(first_products)
    seen = set()
    for p in all_products:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("product_id") or "")
        key = pid or (
            f"{str(p.get('name') or '').strip().lower()}_"
            f"{str(p.get('packsize') or '').strip().lower()}"
        )
        seen.add(key)

    queue: asyncio.Queue = asyncio.Queue()

    async def on_background_response(response):
        if BLINKIT_SEARCH_PATH not in response.url:
            return
        if response.request.method != "POST":
            return
        try:
            body = await response.json()
            await queue.put((response.status, body))
        except Exception:
            pass

    page.on("response", on_background_response)

    try:
        stable_rounds = 0
        previous_total = len(all_products)
        previous_height = await page.evaluate("document.body.scrollHeight")
        reached_end = False

        for scroll_round in range(1, 81):
            # Scroll only AFTER first batch has already been delivered.
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

            deadline = asyncio.get_running_loop().time() + 2.5
            got_response = False

            while asyncio.get_running_loop().time() < deadline:
                timeout = max(
                    0.05,
                    deadline - asyncio.get_running_loop().time(),
                )
                try:
                    status_code, response_body = await asyncio.wait_for(
                        queue.get(),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    break

                if status_code != 200 or not isinstance(response_body, dict):
                    continue

                response_obj = response_body.get("response")
                if not isinstance(response_obj, dict):
                    continue

                snippets = response_obj.get("snippets") or []
                if not snippets:
                    continue

                got_response = True
                batch_products = extract_blinkit_products_from_first_response(
                    response_body,
                    query,
                )

                fresh = []
                for product in batch_products:
                    if not isinstance(product, dict):
                        continue
                    pid = str(product.get("product_id") or "")
                    key = pid or (
                        f"{str(product.get('name') or '').strip().lower()}_"
                        f"{str(product.get('packsize') or '').strip().lower()}"
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    fresh.append(product)
                    all_products.append(product)

                pagination = response_obj.get("pagination")
                if isinstance(pagination, dict):
                    reached_end = bool(pagination.get("end_of_list"))

                logger.info(
                    "[Blinkit FAST] background scroll round=%d | "
                    "snippets=%d | fresh=%d | total=%d | end=%s",
                    scroll_round,
                    len(snippets),
                    len(fresh),
                    len(all_products),
                    reached_end,
                )

                if fresh and on_background_batch is not None:
                    try:
                        result = on_background_batch(list(fresh))
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception(
                            "[Blinkit FAST] background callback failed | round=%d",
                            scroll_round,
                        )

                if reached_end:
                    break

            current_height = await page.evaluate("document.body.scrollHeight")
            current_total = len(all_products)

            if reached_end:
                break

            if current_total == previous_total and current_height == previous_height:
                stable_rounds += 1
            else:
                stable_rounds = 0

            previous_total = current_total
            previous_height = current_height

            logger.info(
                "[Blinkit FAST] background scroll state | round=%d | "
                "response=%s | total=%d | height=%s | stable=%d",
                scroll_round,
                got_response,
                current_total,
                current_height,
                stable_rounds,
            )

            if stable_rounds >= 3:
                logger.info(
                    "[Blinkit FAST] background scroll stable for 3 rounds; stopping | total=%d",
                    len(all_products),
                )
                break

            await page.wait_for_timeout(500)

    except Exception:
        # Preserve everything already collected. The job layer will mark the
        # provider completed-with-data rather than throwing away the first batch.
        logger.exception(
            "[Blinkit FAST] background collection error | query=%r | collected=%d",
            query,
            len(all_products),
        )

    finally:
        try:
            page.remove_listener("response", on_background_response)
        except Exception:
            pass

    logger.info(
        "[Blinkit FAST] full collection complete | query=%r | products=%d",
        query,
        len(all_products),
    )
    return all_products


async def fetch_blinkit_products_streaming(
    query: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    on_first_batch=None,
    on_background_batch=None,
    on_serviceability=None,
    max_retries: int = 3,
) -> List[Dict[str, Any]]:
    """
    QuickCompare streaming contract:

    1. Run the known-good fast Blinkit search.
    2. Deliver its first products immediately through on_first_batch.
    3. Only after that, continue collecting remaining products in background.
    4. Never let background collection alter first-response latency.
    """
    cleaned_q = (query or "").strip()
    if not cleaned_q:
        return []

    f_lat = float(
        latitude if latitude is not None
        else (lat if lat is not None else DEFAULT_LAT)
    )
    f_lon = float(
        longitude if longitude is not None
        else (lon if lon is not None else DEFAULT_LON)
    )

    # Keep retries around the proven first-response path only. Once a first
    # response has been delivered, background errors must not cause another
    # search or duplicate first-batch delivery.
    for attempt in range(max_retries + 1):
        try:
            first_products = await _fast_browser_search(
                cleaned_q,
                f_lat,
                f_lon,
                on_first_batch=on_first_batch,
                on_serviceability=on_serviceability,
            )

            if _BLINKIT_SERVICEABILITY == "unserviceable":
                logger.info("[Blinkit FAST] Explicit serviceability=false; skipping retries/background search.")
                return []

            if not first_products:
                if attempt < max_retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    async with _browser_lock:
                        if _page is not None:
                            await _close_browser_locked()
                    continue
                return []

            # The first batch is already delivered. Everything below is
            # background collection from the same live page/session.
            return await _collect_blinkit_remaining_in_background(
                cleaned_q,
                f_lat,
                f_lon,
                first_products,
                on_background_batch=on_background_batch,
            )

        except Exception as exc:
            logger.warning(
                "[Blinkit FAST] first-response attempt %d/%d failed for %r: %s",
                attempt + 1,
                max_retries + 1,
                cleaned_q,
                exc,
            )
            async with _browser_lock:
                if _page is not None:
                    await _close_browser_locked()
            if attempt < max_retries:
                await asyncio.sleep(0.5 * (2 ** attempt))

    return []


def get_blinkit_serviceability_state() -> Dict[str, str]:
    """Return the last browser-verified Blinkit serviceability state."""
    return {
        "status": _BLINKIT_SERVICEABILITY,
        "message": _BLINKIT_SERVICEABILITY_MESSAGE,
    }


# ---------------------------------------------------------------------------
# Compatibility fetch function used by server.py
# ---------------------------------------------------------------------------

async def fetch_blinkit_products(
    query: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    store_id: Optional[str] = None,
    max_retries: int = 3,
    limit: int = 100,
    max_pages: int = 5,
) -> List[Dict[str, Any]]:
    """
    Production Blinkit search path with full result collection.

    The proven first response is captured first, then the existing Blinkit
    background scroll collector continues loading additional result batches
    from the same authenticated browser page.
    """
    cleaned_q = (query or "").strip()

    if not cleaned_q:
        return []

    f_lat = float(
        latitude
        if latitude is not None
        else (
            lat
            if lat is not None
            else DEFAULT_LAT
        )
    )

    f_lon = float(
        longitude
        if longitude is not None
        else (
            lon
            if lon is not None
            else DEFAULT_LON
        )
    )

    for attempt in range(max_retries + 1):
        try:
            first_products = await _fast_browser_search(
                cleaned_q,
                f_lat,
                f_lon,
            )

            if _BLINKIT_SERVICEABILITY == "unserviceable":
                logger.info(
                    "[Blinkit FAST] Explicit serviceability=false; "
                    "skipping background collection."
                )
                return []

            if not first_products:
                if attempt < max_retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    async with _browser_lock:
                        if _page is not None:
                            await _close_browser_locked()
                    continue
                return []

            # The first response is already available. Continue through
            # Blinkit's lazy-loaded result batches using the existing page.
            # max_pages is retained as the public compatibility knob; the
            # collector currently has its own safety bound of 80 rounds.
            return await _collect_blinkit_remaining_in_background(
                cleaned_q,
                f_lat,
                f_lon,
                first_products,
            )
        except Exception as exc:
            logger.warning(
                "[Blinkit FAST] attempt %d/%d failed for %r: %s",
                attempt + 1,
                max_retries + 1,
                cleaned_q,
                exc,
            )

            # A failed browser/page can be stale. Recreate it before retrying.
            async with _browser_lock:
                if _page is not None:
                    await _close_browser_locked()

            if attempt < max_retries:
                await asyncio.sleep(
                    0.5 * (2 ** attempt)
                    + random.uniform(0.05, 0.15)
                )

    return []


fetch_blinkit = fetch_blinkit_products


# ---------------------------------------------------------------------------
# Existing API compatibility
# ---------------------------------------------------------------------------

class BlinkitPriceRequest(BaseModel):
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    query: str
    store_id: Optional[str] = None


@app.post("/api/get-prices")
async def get_prices(payload: BlinkitPriceRequest):
    lat = (
        payload.latitude
        if payload.latitude is not None
        else payload.lat
    )
    lon = (
        payload.longitude
        if payload.longitude is not None
        else payload.lon
    )

    items = await fetch_blinkit_products(
        query=payload.query,
        latitude=lat,
        longitude=lon,
        store_id=payload.store_id,
    )

    return {
        "query": payload.query,
        "latitude": lat,
        "longitude": lon,
        "count": len(items),
        "products": items,
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "blinkit-persistent-browser-fast",
    }


@app.on_event("shutdown")
async def shutdown_blinkit():
    async with _browser_lock:
        await _close_browser_locked()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "blinkit_service:app",
        host="0.0.0.0",
        port=8002,
        reload=False,
    )
