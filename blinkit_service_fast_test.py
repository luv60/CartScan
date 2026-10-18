
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

        widget_type = str(snippet.get("widget_type", ""))
        if not widget_type.startswith("product_card"):
            continue

        sd = snippet.get("data")
        if not isinstance(sd, dict):
            continue

        cart_item = (
            sd.get("atc_action", {})
            .get("add_to_cart", {})
            .get("cart_item", {})
        )
        if not isinstance(cart_item, dict):
            cart_item = {}

        name_obj = sd.get("name") or sd.get("display_name")
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
        )

        mrp = _money_value(
            cart_item.get("mrp")
            or sd.get("mrp")
            or sd.get("strike_through_price")
        )

        if mrp <= 0 and price > 0:
            mrp = price

        unit_obj = sd.get("variant")
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

        # Keep the old service's minimum-price behavior.
        if price < 5:
            continue

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
) -> List[Dict[str, Any]]:
    page = await _ensure_browser(latitude, longitude)

    async with _search_lock:
        # A previous search may have changed the route, but Blinkit's search
        # input normally remains available on the results page.
        search_box = page.locator(
            'input[placeholder*="Search"], '
            'input[placeholder*="search"], '
            'input[type="search"]'
        ).first

        try:
            await search_box.wait_for(
                state="visible",
                timeout=1_500,
            )
        except Exception:
            container = page.locator(
                '[class*="SearchBar__Container"]'
            ).first

            await container.wait_for(
                state="visible",
                timeout=BROWSER_TIMEOUT_MS,
            )

            await container.click()
            await page.wait_for_timeout(50)

            await search_box.wait_for(
                state="visible",
                timeout=5_000,
            )

        captured: Dict[str, Any] = {
            "response": None,
        }

        async def on_response(response):
            if captured["response"] is not None:
                return

            if BLINKIT_SEARCH_PATH not in response.url:
                return

            if response.request.method != "POST":
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

            products = extract_blinkit_products_from_first_response(
                body,
                query,
            )

            logger.info(
                "[Blinkit FAST] search complete | query=%r | "
                "status=%s | first_response=%.0f ms | products=%d",
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

            return products

        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass


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
    Fast production path.

    Only the first Blinkit search response is consumed. This is intentional:
    QuickCompare's job manager can deliver the first provider batch quickly,
    while a later implementation can add background pagination separately.
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
            return await _fast_browser_search(
                cleaned_q,
                f_lat,
                f_lon,
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
