"""
zepto_service.py

Browser-backed Zepto service for QuickCompare.

IMPORTANT:
- Zepto's real web page/WAF/browser session is used.
- Location is set through the Playwright browser context.
- Serviceability/store is read from the browser's resulting `serviceability`
  cookie after Zepto reloads.
- Product search is performed through the real Zepto UI.
- The service captures the fresh browser POST with mode=SHOW_ALL_RESULTS.
- No direct get_page/serviceability API call is made by this service.
- No hardcoded store IDs or city/store maps are used.
- Browser cookies/WAF tokens are never returned to QuickCompare.

The public symbols used by the existing project are retained.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import suppress
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote

from fastapi import FastAPI
from pydantic import BaseModel

try:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        Response,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
except ImportError:  # pragma: no cover
    Browser = Any
    BrowserContext = Any
    Page = Any
    Response = Any
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None


logger = logging.getLogger("zepto_service")
app = FastAPI(title="Zepto Price Service")

ZEPTO_HOME = "https://www.zepto.com/"
ZEPTO_LOCATION_URL = (
    "https://bff-gateway.zepto.com/api/v1/user/customer/address/location"
)
ZEPTO_SEARCH_URL = (
    "https://bff-gateway.zepto.com/user-search-service/api/v3/search"
)

ZEPTO_IMAGE_CDN = "https://cdn.zeptonow.com/production/inventory/images/"

# Cache values are only accepted from Zepto's live browser serviceability state.
ZEPTO_COORD_STORE_CACHE: Dict[Tuple[float, float], str] = {}
ACTIVE_USER_REGISTRY: Dict[str, Any] = {}

# Reuse one live Zepto browser session instead of launching Chromium and
# repeating location/serviceability setup for every search. The lock is
# required because a single Playwright page cannot safely handle overlapping
# search flows.
_ZEPTO_PERSISTENT_LOCK = asyncio.Lock()
_ZEPTO_PERSISTENT_PW = None
_ZEPTO_PERSISTENT_BROWSER = None
_ZEPTO_PERSISTENT_CONTEXT = None
_ZEPTO_PERSISTENT_PAGE = None
_ZEPTO_PERSISTENT_LOCATION_KEY: Optional[Tuple[float, float]] = None
_ZEPTO_PERSISTENT_STORE_ID = ""
# Serviceability is deliberately separate from scraper success.
# serviceable   = Zepto exposed a live primary store.
# unserviceable = Zepto exposed serviceability state but no live primary store.
# unknown       = serviceability could not be established (WAF/browser/etc.).
_ZEPTO_PERSISTENT_SERVICEABILITY = "unknown"
_ZEPTO_PERSISTENT_SERVICEABILITY_MESSAGE = ""
_ZEPTO_FAST_SEARCH_PAYLOAD: Optional[Dict[str, Any]] = None
_ZEPTO_FAST_SEARCH_HEADERS: Dict[str, str] = {}

# The successful probe used headed Chromium. Keep that behavior.
BROWSER_HEADLESS = False
BROWSER_TIMEOUT_MS = 60_000
WAF_WAIT_MS = 12_000
LOCATION_RETRY_COUNT = 3
SEARCH_TIMEOUT_MS = 20_000


class AuthPhoneRequest(BaseModel):
    phone: str


class AuthOtpRequest(BaseModel):
    phone: str
    otp: str


class PriceRequest(BaseModel):
    latitude: float
    longitude: float
    query: str
    store_id: Optional[str] = None
    pincode: Optional[str] = None
    session_ref_key: Optional[str] = None


class DirectZeptoClient:
    """Compatibility shim for existing callers."""

    def close(self):
        return None


zepto_pool = DirectZeptoClient()


@app.on_event("shutdown")
async def _shutdown_persistent_zepto_session():
    # Gracefully close the reusable Playwright session when FastAPI stops.
    if _ZEPTO_PERSISTENT_LOCK.locked():
        # A running search owns the lock; let it finish rather than interrupting
        # the in-flight request.
        return
    async with _ZEPTO_PERSISTENT_LOCK:
        await _close_persistent_zepto_session()


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes"}:
            return True
        if v in {"false", "0", "no"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def normalize_zepto_price(val: Any, source: str = "rupees") -> float:
    """
    Normalize a price using the representation supplied by the caller.

    `dom` / `rupees`: already INR.
    `subunits` / `api_subunit`: Zepto integer subunits; divide by 100 once.
    """
    if val is None:
        return 0.0

    if isinstance(val, str):
        cleaned = val.replace("₹", "").replace(",", "").strip()
        try:
            return round(float(cleaned), 2)
        except ValueError:
            return 0.0

    try:
        number = float(val)
    except (TypeError, ValueError):
        return 0.0

    if source in {"subunits", "api_subunit"}:
        return round(number / 100.0, 2)

    return round(number, 2)


def extract_packsize_from_title(title: str, current_pack: str = "") -> str:
    current = (current_pack or "").strip()
    generic = {
        "",
        "1 pack",
        "1 pc",
        "1 piece",
        "pack",
        "unit",
        "1 unit",
    }

    if not current or current.lower() in generic:
        match = re.search(
            r"\b(\d+(?:\.\d+)?)\s*"
            r"(ml|l|ltr|litre|litres|liter|kg|kilo|g|gm|grams?)\b",
            title or "",
            re.IGNORECASE,
        )
        if match:
            value = match.group(1)
            unit = match.group(2).lower()

            if unit in {"l", "ltr", "litre", "litres", "liter"}:
                return f"{value} L"
            if unit == "ml":
                return f"{value} ml"
            if unit in {"kg", "kilo"}:
                return f"{value} kg"
            if unit in {"g", "gm", "gram", "grams"}:
                return f"{value} g"

            return f"{value} {unit}"

    return current or "1 pack"


def _normalize_image_url(value: Any, *args: Any) -> str:
    if isinstance(value, dict):
        value = _first_nonempty(
            value.get("path"),
            value.get("imageUrl"),
            value.get("image_url"),
            value.get("url"),
        )

    if not value:
        return ""

    value = str(value).strip()

    # Zepto sometimes provides an image URL/path with an extra
    # product filename appended after the real image extension.
    # Example:
    #   ...uuid.jpeg/Product-Name.jpeg
    # The actual image is only:
    #   ...uuid.jpeg
    match = re.search(
        r"^(.*\.(?:jpeg|jpg|png|webp))(?:/.*)?$",
        value,
        re.IGNORECASE,
    )
    if match:
        value = match.group(1)

    if value.startswith(("http://", "https://")):
        return value

    if value.startswith("//"):
        return "https:" + value

    if value.startswith("/production/"):
        return "https://cdn.zeptonow.com" + value

    if value.startswith("production/"):
        return "https://cdn.zeptonow.com/" + value

    value = value.lstrip("/")

    if value.startswith(("cms/", "inventory/", "ik-seo/")):
        return "https://cdn.zeptonow.com/production/" + value

    return ZEPTO_IMAGE_CDN + value


def _variant_images(variant: Dict[str, Any]) -> List[Any]:
    images = variant.get("images")
    if isinstance(images, list):
        return images
    if isinstance(images, dict):
        return [images]
    return []


def _stock_from_object(obj: Dict[str, Any]) -> bool:
    if obj.get("outOfStock") is True:
        return False
    if obj.get("isOutOfStock") is True:
        return False
    if obj.get("inStock") is False:
        return False
    if obj.get("isAvailable") is False:
        return False
    if obj.get("available") is False:
        return False
    return True


def _coordinate_cache_key(lat: float, lon: float) -> Tuple[float, float]:
    return round(float(lat), 3), round(float(lon), 3)


# ---------------------------------------------------------------------------
# Browser lifecycle
# ---------------------------------------------------------------------------

async def _browser_context(
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> Tuple[Any, Browser, BrowserContext, Page]:
    if async_playwright is None:
        raise RuntimeError(
            "Playwright is not installed. Run "
            "'pip install playwright' and 'playwright install chromium'."
        )

    pw = await async_playwright().start()

    browser = await pw.chromium.launch(
        headless=BROWSER_HEADLESS,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    )

    context = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        # Use the same coordinates for browser geolocation permission that are
        # passed into the service. The location endpoint below remains the
        # authoritative location-setting step.
        geolocation={
            "latitude": float(latitude) if latitude is not None else 0.0,
            "longitude": float(longitude) if longitude is not None else 0.0,
        },
        permissions=["geolocation"],
        locale="en-IN",
        timezone_id="Asia/Kolkata",
    )

    page = await context.new_page()
    page.set_default_timeout(BROWSER_TIMEOUT_MS)

    return pw, browser, context, page


async def _close_persistent_zepto_session() -> None:
    """Close the reusable Zepto browser session, if one exists."""
    global _ZEPTO_PERSISTENT_PW
    global _ZEPTO_PERSISTENT_BROWSER
    global _ZEPTO_PERSISTENT_CONTEXT
    global _ZEPTO_PERSISTENT_PAGE
    global _ZEPTO_PERSISTENT_LOCATION_KEY
    global _ZEPTO_PERSISTENT_STORE_ID
    global _ZEPTO_PERSISTENT_SERVICEABILITY
    global _ZEPTO_PERSISTENT_SERVICEABILITY_MESSAGE
    global _ZEPTO_FAST_SEARCH_PAYLOAD
    global _ZEPTO_FAST_SEARCH_HEADERS

    context = _ZEPTO_PERSISTENT_CONTEXT
    browser = _ZEPTO_PERSISTENT_BROWSER
    pw = _ZEPTO_PERSISTENT_PW

    _ZEPTO_PERSISTENT_PAGE = None
    _ZEPTO_PERSISTENT_CONTEXT = None
    _ZEPTO_PERSISTENT_BROWSER = None
    _ZEPTO_PERSISTENT_PW = None
    _ZEPTO_PERSISTENT_LOCATION_KEY = None
    _ZEPTO_PERSISTENT_STORE_ID = ""
    _ZEPTO_PERSISTENT_SERVICEABILITY = "unknown"
    _ZEPTO_PERSISTENT_SERVICEABILITY_MESSAGE = ""
    _ZEPTO_FAST_SEARCH_PAYLOAD = None
    _ZEPTO_FAST_SEARCH_HEADERS = {}

    if context is not None:
        with suppress(Exception):
            await context.close()
    if browser is not None:
        with suppress(Exception):
            await browser.close()
    if pw is not None:
        with suppress(Exception):
            await pw.stop()


async def _get_persistent_zepto_session(
    lat: float,
    lon: float,
) -> Tuple[Any, Browser, BrowserContext, Page, str]:
    """Return a reusable browser/page already bound to this location."""
    global _ZEPTO_PERSISTENT_PW
    global _ZEPTO_PERSISTENT_BROWSER
    global _ZEPTO_PERSISTENT_CONTEXT
    global _ZEPTO_PERSISTENT_PAGE
    global _ZEPTO_PERSISTENT_LOCATION_KEY
    global _ZEPTO_PERSISTENT_STORE_ID
    global _ZEPTO_PERSISTENT_SERVICEABILITY
    global _ZEPTO_PERSISTENT_SERVICEABILITY_MESSAGE
    global _ZEPTO_FAST_SEARCH_PAYLOAD
    global _ZEPTO_FAST_SEARCH_HEADERS

    key = _coordinate_cache_key(lat, lon)

    if (
        _ZEPTO_PERSISTENT_PAGE is not None
        and not _ZEPTO_PERSISTENT_PAGE.is_closed()
        and _ZEPTO_PERSISTENT_LOCATION_KEY == key
    ):
        return (
            _ZEPTO_PERSISTENT_PW,
            _ZEPTO_PERSISTENT_BROWSER,
            _ZEPTO_PERSISTENT_CONTEXT,
            _ZEPTO_PERSISTENT_PAGE,
            _ZEPTO_PERSISTENT_STORE_ID,
        )

    if _ZEPTO_PERSISTENT_PAGE is not None:
        await _close_persistent_zepto_session()

    _ZEPTO_FAST_SEARCH_PAYLOAD = None
    _ZEPTO_FAST_SEARCH_HEADERS = {}

    pw, browser, context, page = await _browser_context(lat, lon)

    try:
        await _open_zepto(page)
        live_store_id = await _resolve_store_in_existing_browser(
            context,
            page,
            lat,
            lon,
        )

        # _read_live_serviceability_store records the evidence level. Do not
        # infer "unserviceable" merely from an empty product list.
        if live_store_id:
            ZEPTO_COORD_STORE_CACHE[key] = live_store_id
            logger.info(
                "[Zepto] Live serviceability/store established from browser state."
            )
        else:
            if _ZEPTO_PERSISTENT_SERVICEABILITY == "unserviceable":
                logger.info(
                    "[Zepto] Confirmed unserviceable location; skipping product search."
                )
            else:
                logger.warning(
                    "[Zepto] Serviceability cookie did not expose a store ID;"
                    " continuing with browser UI state and live search."
                )

        _ZEPTO_PERSISTENT_PW = pw
        _ZEPTO_PERSISTENT_BROWSER = browser
        _ZEPTO_PERSISTENT_CONTEXT = context
        _ZEPTO_PERSISTENT_PAGE = page
        _ZEPTO_PERSISTENT_LOCATION_KEY = key
        _ZEPTO_PERSISTENT_STORE_ID = live_store_id

        logger.info(
            "[Zepto] Persistent browser session ready for location key %s.",
            key,
        )

        return pw, browser, context, page, live_store_id

    except Exception:
        _ZEPTO_PERSISTENT_SERVICEABILITY = "unknown"
        _ZEPTO_PERSISTENT_SERVICEABILITY_MESSAGE = "Zepto serviceability could not be verified."
        with suppress(Exception):
            await context.close()
        with suppress(Exception):
            await browser.close()
        with suppress(Exception):
            await pw.stop()
        raise


def get_zepto_serviceability_state() -> Dict[str, str]:
    """Return the last browser-verified Zepto serviceability state."""
    return {
        "status": _ZEPTO_PERSISTENT_SERVICEABILITY,
        "message": _ZEPTO_PERSISTENT_SERVICEABILITY_MESSAGE,
    }


async def _open_zepto(page: Page) -> None:
    logger.info("[Zepto] Opening real Zepto browser page.")

    await page.goto(
        ZEPTO_HOME,
        wait_until="domcontentloaded",
        timeout=BROWSER_TIMEOUT_MS,
    )

    # Do not blindly sleep for the full WAF ceiling. Zepto can become
    # interactive earlier, so poll for a usable search/location control.
    # Keep WAF_WAIT_MS as the hard safety ceiling.
    deadline = asyncio.get_running_loop().time() + (WAF_WAIT_MS / 1000.0)

    while asyncio.get_running_loop().time() < deadline:
        try:
            # A mounted search control/input is a strong indication that the
            # Zepto frontend has hydrated enough for the next step.
            if await _find_search_input(page) is not None:
                break

            search_control = page.locator(
                "[aria-label*='search' i], "
                "[data-testid*='search' i], "
                "[data-test*='search' i], "
                "[title*='search' i]"
            ).first

            if await search_control.count() and await search_control.is_visible():
                break

            # Location/delivery controls are also sufficient readiness signals.
            location_control = page.locator(
                "[aria-label*='location' i], "
                "[data-testid*='location' i], "
                "[data-test*='location' i], "
                "[title*='location' i]"
            ).first

            if await location_control.count() and await location_control.is_visible():
                break
        except Exception:
            pass

        await page.wait_for_timeout(250)

    logger.info("[Zepto] Browser session initialized.")


async def _perform_location_ui_flow(page: Page) -> bool:
    """Automate Zepto's location UI using the browser's supplied geolocation."""
    location_triggers = [
        "button[aria-label*='location' i]",
        "[data-testid*='location' i]",
        "button:has-text('Location')",
        "button:has-text('location')",
        "[role='button'][aria-label*='location' i]",
    ]

    trigger = None
    for selector in location_triggers:
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                trigger = loc
                break
        except Exception:
            continue

    if trigger is None:
        try:
            elements = await page.locator("button, a, [role='button']").all()
            for element in elements:
                try:
                    if not await element.is_visible():
                        continue
                    label = " ".join((await element.inner_text()).split()).lower()
                    aria = str(await element.get_attribute("aria-label") or "").lower()
                    title = str(await element.get_attribute("title") or "").lower()
                    combined = f"{label} {aria} {title}"
                    if "location" in combined or "deliver" in combined:
                        trigger = element
                        break
                except Exception:
                    continue
        except Exception:
            pass

    if trigger is None:
        logger.warning("[Zepto] Location UI trigger not found.")
        return False

    try:
        await trigger.click(timeout=8_000)
        logger.info("[Zepto] Location UI opened automatically.")
    except Exception as exc:
        logger.warning("[Zepto] Could not click location UI: %s", exc)
        return False

    await page.wait_for_timeout(1_000)

    current_labels = [
        "Use current location",
        "Use Current Location",
        "Current location",
        "Use my current location",
        "Use my location",
    ]

    current = None
    for label in current_labels:
        try:
            loc = page.get_by_role("button", name=label, exact=False).first
            if await loc.count() and await loc.is_visible():
                current = loc
                break
        except Exception:
            continue
        try:
            loc = page.get_by_text(label, exact=False).first
            if await loc.count() and await loc.is_visible():
                current = loc
                break
        except Exception:
            continue

    if current is None:
        logger.warning("[Zepto] Current-location action not found after opening location UI.")
        return False

    try:
        await current.click(timeout=8_000)
        logger.info("[Zepto] Current-location action clicked automatically.")
    except Exception as exc:
        logger.warning("[Zepto] Could not click current-location action: %s", exc)
        return False

    # Wait only until the UI has had a chance to apply the location. The
    # subsequent serviceability poll is authoritative, so there is no need for
    # a fixed 3-second sleep here.
    await page.wait_for_timeout(500)
    return True


# ---------------------------------------------------------------------------
# Location/serviceability
# ---------------------------------------------------------------------------

async def _set_location_in_browser(
    context: BrowserContext,
    page: Page,
    lat: float,
    lon: float,
) -> Dict[str, Any]:
    """Set Zepto location through the real browser UI only.

    Zepto's former location HTTP endpoint is currently returning HTTP 426.
    The browser context already has the requested coordinates and geolocation
    permission, so the supported path here is to let the page request the
    browser location and then use Zepto's own "Use current location" UI.

    No direct Zepto location HTTP request is made from Python.
    """
    geo: Dict[str, Any] = {}

    try:
        geo = await page.evaluate("""() => new Promise((resolve, reject) => {
            navigator.geolocation.getCurrentPosition(
                p => resolve({
                    latitude: p.coords.latitude,
                    longitude: p.coords.longitude,
                    accuracy: p.coords.accuracy
                }),
                e => reject(e),
                { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
            );
        })""")
        logger.info(
            "[Zepto] Browser geolocation confirmed: %.6f, %.6f (accuracy=%s)",
            float(geo.get("latitude") or 0),
            float(geo.get("longitude") or 0),
            geo.get("accuracy"),
        )
    except Exception as exc:
        logger.warning("[Zepto] Browser geolocation check failed: %s", exc)

    ui_ok = await _perform_location_ui_flow(page)
    if not ui_ok:
        raise RuntimeError("Zepto location UI flow could not be completed")

    return {
        "latitude": float(lat),
        "longitude": float(lon),
        "browser_geolocation": geo,
        "ui_flow_completed": True,
    }


def _extract_store_from_serviceability_cookie(value: str) -> str:
    """
    The verified browser probe reads the `serviceability` cookie after reload.

    Current verified shape:
        {
          "primaryStore": {
             "storeId": "...",
             ...
          },
          ...
        }
    """
    if not value:
        return ""

    candidates = [value]

    # Cookie values can be URL encoded more than once.
    for _ in range(2):
        try:
            decoded = unquote(candidates[-1])
        except Exception:
            break
        if decoded == candidates[-1]:
            break
        candidates.append(decoded)

    for candidate in reversed(candidates):
        try:
            data = json.loads(candidate)
        except Exception:
            continue

        if not isinstance(data, dict):
            continue

        primary = data.get("primaryStore")
        if isinstance(primary, dict):
            store_id = _first_nonempty(
                primary.get("storeId"),
                primary.get("store_id"),
            )
            if store_id:
                return str(store_id)

        # Defensive recursive fallback, still only inside serviceability data.
        found = _find_serviceable_store_id(data)
        if found:
            return found

    return ""


def _find_serviceable_store_id(data: Any) -> str:
    candidates: List[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            store_id = _first_nonempty(
                obj.get("storeId"),
                obj.get("store_id"),
                obj.get("storeID"),
            )

            serviceable = _first_nonempty(
                obj.get("serviceable") if "serviceable" in obj else None,
                obj.get("isServiceable") if "isServiceable" in obj else None,
            )

            if store_id and serviceable is True:
                candidates.insert(0, str(store_id))

            for value in obj.values():
                walk(value)

        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data)
    return candidates[0] if candidates else ""


async def _read_live_serviceability_store(
    context: BrowserContext,
    page: Page,
    *,
    reload_page: bool = True,
) -> str:
    """
    Do NOT call Zepto get_page here.

    The successful probe established that after location + reload, Zepto's
    frontend creates the serviceability cookie containing the live primary store.
    """
    global _ZEPTO_PERSISTENT_SERVICEABILITY
    global _ZEPTO_PERSISTENT_SERVICEABILITY_MESSAGE

    cookie_seen = False

    if reload_page:
        logger.info("[Zepto] Reloading Zepto after location selection.")

        await page.reload(
            wait_until="domcontentloaded",
            timeout=BROWSER_TIMEOUT_MS,
        )

    # Poll immediately for the live serviceability cookie instead of sleeping
    # 7 seconds first. The old delay made every request pay the full cost even
    # when Zepto had already established the store.
    deadline = asyncio.get_running_loop().time() + 10.0

    while asyncio.get_running_loop().time() < deadline:
        cookies = await context.cookies()

        for cookie in cookies:
            if cookie.get("name") != "serviceability":
                continue

            cookie_seen = True

            store_id = _extract_store_from_serviceability_cookie(
                str(cookie.get("value") or "")
            )

            if store_id:
                _ZEPTO_PERSISTENT_SERVICEABILITY = "serviceable"
                _ZEPTO_PERSISTENT_SERVICEABILITY_MESSAGE = "Zepto is serviceable at this location."
                logger.info("[Zepto] Live serviceability/store established.")
                return store_id

        await page.wait_for_timeout(250)

    if cookie_seen:
        _ZEPTO_PERSISTENT_SERVICEABILITY = "unserviceable"
        _ZEPTO_PERSISTENT_SERVICEABILITY_MESSAGE = "Zepto is not serviceable at this location."
        logger.info("[Zepto] Location is not serviceable: serviceability state had no primary store.")
    else:
        _ZEPTO_PERSISTENT_SERVICEABILITY = "unknown"
        _ZEPTO_PERSISTENT_SERVICEABILITY_MESSAGE = "Zepto serviceability could not be verified."
        logger.warning(
            "[Zepto] serviceability cookie did not contain a live primary store and was not observed."
        )
    return ""


async def _resolve_store_in_existing_browser(
    context: BrowserContext,
    page: Page,
    lat: float,
    lon: float,
) -> str:
    await _set_location_in_browser(context, page, lat, lon)
    return await _read_live_serviceability_store(
        context,
        page,
        reload_page=True,
    )


async def resolve_zepto_store_id(
    lat: float,
    lon: float,
    *args,
    **kwargs,
) -> str:
    """
    Resolve the live Zepto store for coordinates.

    This standalone resolver uses the same proven browser flow:
        Zepto -> location endpoint -> reload -> serviceability cookie.

    No get_page request and no hardcoded store map.
    """
    try:
        f_lat = float(lat)
        f_lon = float(lon)
    except (TypeError, ValueError):
        logger.warning("[Zepto] Invalid coordinates.")
        return ""

    if f_lat == 0.0 and f_lon == 0.0:
        logger.warning("[Zepto] Missing coordinates.")
        return ""

    cache_key = _coordinate_cache_key(f_lat, f_lon)

    cached = ZEPTO_COORD_STORE_CACHE.get(cache_key)
    if cached:
        logger.info("[Zepto] Using cached live store resolution.")
        return cached

    pw = browser = context = page = None

    try:
        pw, browser, context, page = await _browser_context(
            f_lat,
            f_lon,
        )

        await _open_zepto(page)

        store_id = await _resolve_store_in_existing_browser(
            context,
            page,
            f_lat,
            f_lon,
        )

        if not store_id:
            logger.info("[Zepto] Location is not serviceable.")
            return ""

        ZEPTO_COORD_STORE_CACHE[cache_key] = store_id
        logger.info("[Zepto] Dynamic store resolution succeeded.")

        return store_id

    except Exception as exc:
        logger.warning(
            "[Zepto] Dynamic store resolution failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return ""

    finally:
        if context is not None:
            with suppress(Exception):
                await context.close()
        if browser is not None:
            with suppress(Exception):
                await browser.close()
        if pw is not None:
            with suppress(Exception):
                await pw.stop()


async def resolve_dark_store(session, lat: float, lon: float) -> str:
    # `session` is retained for compatibility; browser state is authoritative.
    return await resolve_zepto_store_id(lat, lon)


async def resolve_zepto_dark_store(
    lat: float,
    lon: float,
    session=None,
) -> Dict[str, Any]:
    store_id = await resolve_zepto_store_id(lat, lon)

    return {
        "store_id": store_id,
        "pincode": "",
        "latitude": lat,
        "longitude": lon,
    }


def build_zepto_headers(meta: Dict[str, Any]) -> Dict[str, str]:
    return {
        "x-store-id": str(meta.get("store_id", "")),
        "x-user-latitude": str(meta.get("latitude", "")),
        "x-user-longitude": str(meta.get("longitude", "")),
        "x-pincode": str(meta.get("pincode", "")),
        "tenant_id": "ZEPTO",
    }


# ---------------------------------------------------------------------------
# Product parser
# ---------------------------------------------------------------------------

def _looks_like_verified_product_response(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("product"), dict)
        and isinstance(obj.get("productVariant"), dict)
    )


def _candidate_verified_product_responses(
    data: Any,
) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    seen: Set[int] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            object_id = id(obj)
            if object_id in seen:
                return
            seen.add(object_id)

            if _looks_like_verified_product_response(obj):
                found.append(obj)

            for value in obj.values():
                walk(value)

        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data)
    return found


def _normalize_zepto_product_url(url: Any) -> str:
    """Return a usable absolute Zepto product URL from a live payload field."""
    if not url:
        return ""
    value = str(url).strip()
    if not value:
        return ""
    if value.startswith("/"):
        return f"https://www.zepto.com{value}"
    if value.startswith(("https://www.zepto.com/", "https://www.zeptonow.com/")):
        return value
    if value.startswith("http://www.zepto.com/"):
        return value.replace("http://", "https://", 1)
    if value.startswith("http://www.zeptonow.com/"):
        return value.replace("http://", "https://", 1)
    return ""


def _build_zepto_product_url(
    item: Dict[str, Any],
    product: Dict[str, Any],
    variant: Dict[str, Any],
    variant_id: str,
) -> str:
    """Prefer a canonical payload URL; otherwise use Zepto's variant route."""
    for source in (item, variant, product):
        if not isinstance(source, dict):
            continue
        for key in (
            "url",
            "productUrl",
            "product_url",
            "canonicalUrl",
            "canonical_url",
            "seoUrl",
            "seo_url",
            "webUrl",
            "web_url",
            "href",
        ):
            resolved = _normalize_zepto_product_url(source.get(key))
            if "/pn/" in resolved and "/pvid/" in resolved:
                return resolved

    # Zepto's live product has a variant UUID. The existing Flutter code uses
    # this direct-product route, so avoid sending users to search results.
    if variant_id:
        return f"https://www.zeptonow.com/pn/x/pvid/{variant_id}"
    return ""


def _extract_verified_product(
    item: Dict[str, Any],
    target_store_id: str = "",
) -> Optional[Dict[str, Any]]:
    product = item.get("product")
    variant = item.get("productVariant")

    if not isinstance(product, dict) or not isinstance(variant, dict):
        return None

    store_id = str(
        _first_nonempty(
            item.get("storeId"),
            item.get("store_id"),
            variant.get("storeId"),
            product.get("storeId"),
            target_store_id,
            "",
        )
        or ""
    )

    # If Zepto explicitly identifies another store, don't mix it in.
    explicit_store = _first_nonempty(
        item.get("storeId"),
        item.get("store_id"),
        variant.get("storeId"),
        product.get("storeId"),
    )
    if (
        target_store_id
        and explicit_store
        and str(explicit_store) != str(target_store_id)
    ):
        return None

    name = _first_nonempty(
        product.get("name"),
        product.get("productName"),
        item.get("name"),
        item.get("productName"),
    )

    if not name:
        return None

    brand = _first_nonempty(
        product.get("brand"),
        product.get("brandName"),
        item.get("brand"),
        "",
    )

    raw_price = _first_nonempty(
        item.get("sellingPrice"),
        variant.get("sellingPrice"),
        item.get("discountedSellingPrice"),
        variant.get("discountedSellingPrice"),
        item.get("discountedPrice"),
        variant.get("discountedPrice"),
    )

    raw_mrp = _first_nonempty(
        item.get("mrp"),
        variant.get("mrp"),
        product.get("mrp"),
        raw_price,
    )

    price = normalize_zepto_price(raw_price, source="subunits")
    mrp = normalize_zepto_price(raw_mrp, source="subunits")

    if price <= 0 and mrp <= 0:
        return None

    variant_id = str(
        _first_nonempty(
            variant.get("id"),
            variant.get("variantId"),
            item.get("variantId"),
            "",
        )
        or ""
    )

    product_id = str(
        _first_nonempty(
            item.get("objectId"),
            item.get("productId"),
            product.get("id"),
            product.get("productId"),
            "",
        )
        or ""
    )

    packsize = str(
        _first_nonempty(
            variant.get("formattedPacksize"),
            variant.get("packsize"),
            variant.get("unit"),
            variant.get("quantity"),
            item.get("formattedPacksize"),
            "",
        )
        or ""
    ).strip()

    if not packsize:
        packsize = extract_packsize_from_title(str(name), "")

    image = ""
    images = _variant_images(variant)
    if images:
        image = _normalize_image_url(images[0], str(name))

    if not image:
        image = _normalize_image_url(
            _first_nonempty(
                variant.get("imageUrl"),
                variant.get("image_url"),
                product.get("imageUrl"),
                product.get("image_url"),
                "",
            )
        )

    available_quantity = _first_nonempty(
        item.get("availableQuantity"),
        variant.get("availableQuantity"),
        None,
    )

    is_available = (
        _stock_from_object(item)
        and _stock_from_object(variant)
    )

    if available_quantity is not None:
        try:
            if float(available_quantity) <= 0:
                is_available = False
        except (TypeError, ValueError):
            pass

    buy_url = _build_zepto_product_url(
        item=item,
        product=product,
        variant=variant,
        variant_id=variant_id,
    )

    return {
        "id": product_id,
        "product_id": product_id,
        "variant_id": variant_id,
        "name": str(name),
        "brand": str(brand or ""),
        "price": price,
        "discounted_price": price,
        "discountedSellingPrice": price,
        "selling_price": price,
        "mrp": mrp,
        "packsize": packsize,
        "image_url": image,
        "buy_url": buy_url,
        "store": "Zepto",
        "store_id": store_id,
        "available_quantity": available_quantity,
        "is_available": bool(is_available),
        "out_of_stock": not bool(is_available),
    }


def _legacy_product_from_object(
    obj: Dict[str, Any],
    target_store_id: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Compatibility parser for the project's unit-test schemas and older Zepto
    payloads. The verified live browser payload uses _extract_verified_product.
    """
    name = _first_nonempty(
        obj.get("name"),
        obj.get("product_name"),
        obj.get("title"),
    )

    if not name:
        return None

    # Don't interpret arbitrary layout metadata as a product.
    raw_price = _first_nonempty(
        obj.get("discountedSellingPrice"),
        obj.get("sellingPrice"),
        obj.get("discountedPrice"),
        obj.get("price"),
        obj.get("mrp"),
    )

    if raw_price is None:
        return None

    has_price_signal = any(
        key in obj
        for key in (
            "discountedSellingPrice",
            "sellingPrice",
            "discountedPrice",
            "price",
            "mrp",
        )
    )
    if not has_price_signal:
        return None

    if "formattedSellingPrice" in obj or "formattedPrice" in obj:
        price = normalize_zepto_price(
            _first_nonempty(
                obj.get("formattedSellingPrice"),
                obj.get("formattedPrice"),
            ),
            source="dom",
        )
        mrp = normalize_zepto_price(
            _first_nonempty(
                obj.get("formattedMrp"),
                obj.get("mrp"),
                raw_price,
            ),
            source="dom",
        )
    else:
        subunit_field = any(
            key in obj
            for key in (
                "discountedSellingPrice",
                "sellingPrice",
                "discountedPrice",
            )
        )
        # Existing tests and old raw Zepto layouts use these API fields in
        # subunits.
        source = "subunits" if subunit_field else "rupees"
        price = normalize_zepto_price(raw_price, source=source)
        mrp = normalize_zepto_price(
            _first_nonempty(
                obj.get("mrp"),
                obj.get("originalPrice"),
                raw_price,
            ),
            source=source,
        )

    if price <= 0:
        return None

    variant = obj.get("productVariant")
    if not isinstance(variant, dict):
        variant = {}

    product_id = str(
        _first_nonempty(
            obj.get("id"),
            obj.get("productId"),
            obj.get("objectId"),
            "",
        )
        or ""
    )

    variant_id = str(
        _first_nonempty(
            variant.get("id"),
            variant.get("variantId"),
            obj.get("variantId"),
            "",
        )
        or ""
    )

    packsize = str(
        _first_nonempty(
            obj.get("packsize"),
            obj.get("unit"),
            obj.get("formattedPacksize"),
            obj.get("quantity"),
            variant.get("formattedPacksize"),
            "1 unit",
        )
    )

    image = _normalize_image_url(
        _first_nonempty(
            obj.get("imageUrl"),
            obj.get("image_url"),
            obj.get("image"),
            (
                _variant_images(variant)[0]
                if _variant_images(variant)
                else ""
            ),
        )
    )

    is_available = _stock_from_object(obj)
    if variant:
        is_available = is_available and _stock_from_object(variant)

    store_id = str(
        _first_nonempty(
            obj.get("storeId"),
            obj.get("store_id"),
            obj.get("storeID"),
            target_store_id,
            "",
        )
        or ""
    )

    buy_url = _build_zepto_product_url(
        item=obj,
        product=obj,
        variant=variant,
        variant_id=variant_id,
    )

    return {
        "id": product_id,
        "product_id": product_id,
        "variant_id": variant_id,
        "name": str(name),
        "brand": str(
            _first_nonempty(
                obj.get("brand"),
                obj.get("brandName"),
                "",
            )
            or ""
        ),
        "price": price,
        "discounted_price": price,
        "discountedSellingPrice": price,
        "selling_price": price,
        "mrp": mrp,
        "packsize": packsize,
        "image_url": image,
        "buy_url": buy_url,
        "store": "Zepto",
        "store_id": store_id,
        "is_available": is_available,
        "out_of_stock": not is_available,
    }


def extract_products(
    data: Any,
    store_id: str = "",
) -> List[Dict[str, Any]]:
    """
    Canonical Zepto parser.

    Live payload:
        ...productResponse
          ├── product
          └── productVariant

    Legacy compatibility:
        layout/widgets/products arrays containing flat product objects.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return []

    if not isinstance(data, (dict, list)):
        return []

    products: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str]] = set()

    # 1. Verified live response shape.
    for item in _candidate_verified_product_responses(data):
        product = _extract_verified_product(item, store_id)
        if not product:
            continue

        key = (
            str(product.get("product_id", "")),
            str(product.get("variant_id", "")),
            str(product.get("name", "")).strip().lower(),
        )

        if key not in seen:
            seen.add(key)
            products.append(product)

    # 2. Legacy/unit-test fallback only if live shape produced nothing.
    if not products:
        def walk(obj: Any) -> None:
            if isinstance(obj, dict):
                # Do not treat verified productResponse as a second product.
                if not _looks_like_verified_product_response(obj):
                    product = _legacy_product_from_object(obj, store_id)
                    if product:
                        key = (
                            str(product.get("product_id", "")),
                            str(product.get("variant_id", "")),
                            str(product.get("name", "")).strip().lower(),
                        )
                        if key not in seen:
                            seen.add(key)
                            products.append(product)

                for value in obj.values():
                    walk(value)

            elif isinstance(obj, list):
                for value in obj:
                    walk(value)

        walk(data)

    logger.info("[Zepto Parser] Extracted %d product items.", len(products))
    return products


def parse_zepto_api_json(
    data: Any,
    query: str = "",
    *args,
    **kwargs,
) -> List[Dict[str, Any]]:
    store_id = kwargs.get("store_id", "")

    if not store_id and args and isinstance(args[0], str):
        store_id = args[0]

    return extract_products(data, store_id=store_id)


def parse_zepto_html_payload(
    html_str: str,
    query: str = "",
) -> List[Dict[str, Any]]:
    try:
        data = json.loads(html_str)
    except Exception:
        return []

    return extract_products(data)


# ---------------------------------------------------------------------------
# Browser search
# ---------------------------------------------------------------------------

async def _open_search_ui(page: Page) -> bool:
    """Open Zepto's search UI, tolerating delayed frontend hydration.

    Zepto can take a few seconds after the location-triggered reload to mount
    the search control. The previous implementation checked once, ~1 second
    after serviceability was established, and therefore could report
    "Search UI trigger not found" even though the search control appeared
    moments later.

    This helper:
      1. Reuses an already-visible search input if Zepto has mounted it.
      2. Polls semantic search controls for a short bounded period.
      3. Falls back to visible elements carrying search-related attributes,
         including div-based controls.
      4. Waits for the actual input to appear after clicking.

    Location/serviceability behavior is unchanged.
    """
    search_selectors = [
        "button[aria-label*='search' i]",
        "[role='button'][aria-label*='search' i]",
        "[aria-label*='search' i]",
        "[data-testid*='search' i]",
        "[data-test*='search' i]",
        "button[title*='search' i]",
        "[role='button'][title*='search' i]",
        "[title*='search' i]",
        "a[aria-label*='search' i]",
        "button:has-text('Search')",
        "[role='button']:has-text('Search')",
    ]

    deadline = asyncio.get_running_loop().time() + 5.0

    while asyncio.get_running_loop().time() < deadline:
        # If the input is already mounted, there is nothing left to open.
        existing_input = await _find_search_input(page)
        if existing_input is not None:
            logger.info("[Zepto] Search input already available.")
            return True

        # First try semantic/attribute-based controls.
        for selector in search_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=3_000)
                    logger.info("[Zepto] Search UI opened automatically.")

                    # Do not assume a fixed 1 second is enough; wait for the
                    # actual input that the next step needs.
                    input_deadline = asyncio.get_running_loop().time() + 3.0
                    while asyncio.get_running_loop().time() < input_deadline:
                        if await _find_search_input(page) is not None:
                            return True
                        await page.wait_for_timeout(250)

                    break
            except Exception:
                continue

        # Broader DOM fallback. Some Zepto builds render the search control
        # as a div/span rather than a button or role=button.
        try:
            elements = await page.locator(
                "[aria-label], [title], [data-testid], [data-test], "
                "[class*='search' i], button, a, [role='button'], [role='link']"
            ).all()

            for element in elements:
                try:
                    if not await element.is_visible():
                        continue

                    label = " ".join((await element.inner_text()).split()).lower()
                    aria = str(
                        await element.get_attribute("aria-label") or ""
                    ).lower()
                    title = str(
                        await element.get_attribute("title") or ""
                    ).lower()
                    testid = str(
                        await element.get_attribute("data-testid") or ""
                    ).lower()
                    test = str(
                        await element.get_attribute("data-test") or ""
                    ).lower()
                    classes = str(
                        await element.get_attribute("class") or ""
                    ).lower()

                    # Prefer explicit metadata. Class-name matching is only a
                    # fallback because many page elements can contain
                    # "search" in generated class names.
                    explicit_search = (
                        "search" in aria
                        or "search" in title
                        or "search" in testid
                        or "search" in test
                    )
                    text_search = label.strip() in {
                        "search",
                        "search products",
                        "search for products",
                    }
                    class_search = "search" in classes

                    if explicit_search or text_search or class_search:
                        await element.click(timeout=3_000)
                        logger.info("[Zepto] Search UI opened automatically.")

                        input_deadline = asyncio.get_running_loop().time() + 3.0
                        while asyncio.get_running_loop().time() < input_deadline:
                            if await _find_search_input(page) is not None:
                                return True
                            await page.wait_for_timeout(250)
                except Exception:
                    continue

        except Exception:
            pass

        await page.wait_for_timeout(250)

    logger.warning("[Zepto] Search UI trigger not found.")
    return False


async def _find_search_input(page: Page):
    selectors = [
        "input[placeholder*='Search for over']",
        "input[placeholder*='Search' i]",
        "input[type='search']",
        "input",
    ]

    # Immediate probe only. Callers that need to wait already perform their
    # own bounded polling. Keeping this helper non-blocking avoids accidentally
    # adding several seconds to every readiness check.
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                return locator
        except Exception:
            continue

    return None


async def _click_show_all_results(page: Page, query: str) -> bool:
    """
    Match the exact UI action proven by zepto_service_probe.py.
    """
    exact_texts = [
        f"Show all results for\n{query}",
        f"Show all results for {query}",
        "Show all results for",
    ]

    for text in exact_texts:
        try:
            locator = page.get_by_text(text, exact=False)
            count = await locator.count()

            for i in range(count):
                candidate = locator.nth(i)

                try:
                    if not await candidate.is_visible():
                        continue

                    await candidate.click(timeout=5_000)
                    logger.info("[Zepto] Clicked Show All Results.")
                    return True

                except Exception:
                    continue

        except Exception:
            continue

    # DOM fallback used by the successful probe.
    try:
        elements = await page.locator("div,span,p,a,button").all()

        target = f"show all results for {query}".lower()

        for element in elements:
            try:
                if not await element.is_visible():
                    continue

                text = (await element.inner_text()).strip()
                normalized = " ".join(text.split()).lower()

                if (
                    normalized == target
                    or (
                        "show all results for" in normalized
                        and query.lower() in normalized
                    )
                ):
                    await element.click(timeout=5_000)
                    logger.info("[Zepto] Clicked Show All Results.")
                    return True

            except Exception:
                continue

    except Exception:
        pass

    return False


async def _perform_fast_browser_search(
    context: BrowserContext,
    query: str,
) -> Optional[Dict[str, Any]]:
    """Replay the already-proven Zepto search request inside the live browser context.

    The first real UI search captures Zepto's current request payload/headers.
    Subsequent searches reuse that live browser session and call the same
    first-party endpoint through Playwright's context request client. Cookies
    remain inside the browser context and are never returned to the app.
    """
    global _ZEPTO_FAST_SEARCH_PAYLOAD
    global _ZEPTO_FAST_SEARCH_HEADERS

    if not _ZEPTO_FAST_SEARCH_PAYLOAD:
        return None

    payload = dict(_ZEPTO_FAST_SEARCH_PAYLOAD)
    payload["query"] = query
    payload["pageNumber"] = 0
    payload["mode"] = "SHOW_ALL_RESULTS"

    logger.info(
        "[Zepto DEBUG] FAST SEARCH query=%r payload_query=%r",
        query,
        payload.get("query"),
    )

    headers = dict(_ZEPTO_FAST_SEARCH_HEADERS)
    headers.pop("content-length", None)
    headers.pop("host", None)
    headers.pop("cookie", None)
    headers["content-type"] = "application/json"
    headers["accept"] = "application/json, text/plain, */*"

    try:
        response = await context.request.post(
            ZEPTO_SEARCH_URL,
            data=json.dumps(payload, separators=(",", ":")),
            headers=headers,
            timeout=SEARCH_TIMEOUT_MS,
        )

        if response.status != 200:
            logger.info(
                "[Zepto] Fast search returned HTTP %s; falling back to UI.",
                response.status,
            )
            return None

        body = await response.json()

        try:
            debug_products = extract_products(body)
            logger.info(
                "[Zepto DEBUG] FAST RESULT query=%r first_product=%r count=%d",
                query,
                debug_products[0].get("name") if debug_products else "",
                len(debug_products),
            )
        except Exception:
            pass

        if isinstance(body, dict):
            logger.info("[Zepto] Fast search request returned successfully.")
            return body

    except Exception as exc:
        logger.info(
            "[Zepto] Fast search failed; falling back to UI: %s: %s",
            type(exc).__name__,
            exc,
        )

    return None


async def _perform_store_bound_search_fallback(
    page: Page,
    query: str,
    latitude: Optional[float],
    longitude: Optional[float],
    store_id: str,
) -> Optional[List[Dict[str, Any]]]:
    """
    Last-resort search path inside the SAME live Zepto browser page.

    The normal path is still the real Zepto UI. If Zepto's current frontend
    does not mount a searchable <input>, use the exact first-party search POST
    that the browser itself uses, with the browser's live cookies/session.

    This is deliberately executed with page.evaluate(fetch(...)) rather than
    curl/requests, so WAF/session/location state stays inside the real browser.
    """
    if not store_id or latitude is None or longitude is None:
        logger.warning(
            "[Zepto] Store-bound search fallback skipped: missing live store/location."
        )
        return None

    script = """
    async ({query, latitude, longitude, storeId}) => {
      const url =
        "https://bff-gateway.zepto.com/user-search-service/api/v3/search";

      const headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "tenant_id": "ZEPTO",
        "store_id": String(storeId),
        "x-store-id": String(storeId),
        "x-user-latitude": String(latitude),
        "x-user-longitude": String(longitude)
      };

      let userSessionId = "";
      const pages = [];

      for (let pageNumber = 0; pageNumber < 10; pageNumber++) {
        const payload = {
          query: String(query),
          pageNumber: pageNumber,
          mode: "SHOW_ALL_RESULTS",
          userSessionId: userSessionId
        };

        const response = await fetch(url, {
          method: "POST",
          credentials: "include",
          headers,
          body: JSON.stringify(payload)
        });

        const text = await response.text();

        let body = null;
        try {
          body = JSON.parse(text);
        } catch (_) {
          body = null;
        }

        if (!response.ok || !body || typeof body !== "object") {
          return {
            ok: false,
            status: response.status,
            pages
          };
        }

        pages.push(body);

        if (body.userSessionId) {
          userSessionId = String(body.userSessionId);
        }

        const pageCount = Number(body.pageProductCount || 0);
        const totalCount = Number(body.totalProductCount || 0);
        const reachedEnd = body.hasReachedEnd === true;

        if (reachedEnd || pageCount <= 0) {
          break;
        }

        const retrieved = pages.reduce(
          (sum, item) => sum + Number(item.pageProductCount || 0),
          0
        );

        if (totalCount > 0 && retrieved >= totalCount) {
          break;
        }
      }

      return {
        ok: true,
        status: 200,
        pages
      };
    }
    """

    try:
        result = await page.evaluate(
            script,
            {
                "query": query,
                "latitude": float(latitude),
                "longitude": float(longitude),
                "storeId": str(store_id),
            },
        )

        if not isinstance(result, dict) or not result.get("ok"):
            logger.warning(
                "[Zepto] Store-bound search fallback failed | status=%s",
                result.get("status") if isinstance(result, dict) else "unknown",
            )
            return None

        pages = result.get("pages") or []
        valid_pages = [p for p in pages if isinstance(p, dict)]

        if not valid_pages:
            logger.warning("[Zepto] Store-bound search fallback returned no result pages.")
            return None

        products = extract_products(valid_pages)
        logger.info(
            "[Zepto] Store-bound search fallback succeeded | pages=%d products=%d",
            len(valid_pages),
            len(products),
        )
        return valid_pages

    except Exception as exc:
        logger.warning(
            "[Zepto] Store-bound search fallback exception: %s: %s",
            type(exc).__name__,
            exc,
        )
        return None


async def _perform_browser_search(
    page: Page,
    query: str,
    context: Optional[BrowserContext] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    store_id: str = "",
) -> Any:
    """Search Zepto through the real UI and scroll through the lazy-loaded feed.

    Zepto's search results are a vertically lazy-loaded product feed. The first
    response contains only the initially loaded products; additional search
    responses are triggered as the browser scrolls down. We therefore keep the
    real browser UI active and collect every matching search response generated
    while scrolling instead of manually requesting page numbers.
    """
    global _ZEPTO_FAST_SEARCH_PAYLOAD
    global _ZEPTO_FAST_SEARCH_HEADERS

    search_responses: List[Dict[str, Any]] = []
    search_started = False

    async def on_response(response: Response) -> None:
        nonlocal search_started

        try:
            if "/user-search-service/api/v3/search" not in response.url:
                return

            request = response.request
            if request.method != "POST":
                return

            post_data = request.post_data or ""

            try:
                payload = json.loads(post_data)
            except Exception:
                payload = {}

            mode = str(payload.get("mode") or "")
            response_query = str(payload.get("query") or "").strip()

            if (
                not search_started
                or mode != "SHOW_ALL_RESULTS"
                or response_query.lower() != query.strip().lower()
            ):
                return

            if response.status != 200:
                return

            body = await response.json()

            if isinstance(body, dict):
                # Keep the live request shape for diagnostics/compatibility,
                # but do not replay page numbers. Scrolling the real page is
                # what causes Zepto's lazy loader to request the next results.
                try:
                    captured_headers = await request.all_headers()
                except Exception:
                    captured_headers = {}

                _ZEPTO_FAST_SEARCH_PAYLOAD = dict(payload)
                _ZEPTO_FAST_SEARCH_HEADERS = {
                    str(k).lower(): str(v)
                    for k, v in captured_headers.items()
                    if str(k).lower() not in {
                        "content-length",
                        "host",
                        "cookie",
                    }
                }

                search_responses.append(
                    {
                        "payload": payload,
                        "body": body,
                    }
                )

                try:
                    products = extract_products(body)
                    current_page = body.get("currentPage")
                    total_count = body.get("totalProductCount")
                    has_end = body.get("hasReachedEnd") is True
                    logger.info(
                        "[Zepto] Scroll response currentPage=%r pageProducts=%d totalProducts=%r hasReachedEnd=%s",
                        current_page,
                        len(products),
                        total_count,
                        has_end,
                    )
                except Exception:
                    pass

        except Exception:
            return

    response_handler = lambda response: asyncio.create_task(on_response(response))
    page.on("response", response_handler)

    try:
        ui_opened = await _open_search_ui(page)

        search_input = None
        if ui_opened:
            for _ in range(20):
                search_input = await _find_search_input(page)
                if search_input is not None:
                    break
                await page.wait_for_timeout(250)

        # Zepto sometimes has a hydrated page but no mounted search input.
        # Do not declare the provider failed in that case: use the same
        # store-bound search API from inside the live browser context.
        if search_input is None:
            fallback_pages = await _perform_store_bound_search_fallback(
                page,
                query,
                latitude,
                longitude,
                store_id or _ZEPTO_PERSISTENT_STORE_ID,
            )
            if fallback_pages:
                return fallback_pages if len(fallback_pages) > 1 else fallback_pages[0]
            raise RuntimeError("Zepto search UI and store-bound search fallback both failed.")

        await search_input.click()
        await search_input.fill("")
        await search_input.type(query, delay=100)
        await page.wait_for_timeout(750)

        search_started = True
        clicked = await _click_show_all_results(page, query)

        if not clicked:
            await search_input.press("Enter")

        deadline = asyncio.get_running_loop().time() + (
            SEARCH_TIMEOUT_MS / 1000.0
        )

        # Wait for the initial SHOW_ALL_RESULTS response before starting the
        # scroll loop. This prevents scrolling the autocomplete/search screen.
        while (
            not search_responses
            and asyncio.get_running_loop().time() < deadline
        ):
            await page.wait_for_timeout(100)

        if not search_responses:
            raise RuntimeError(
                "Timed out waiting for fresh Zepto SHOW_ALL_RESULTS response."
            )

        # The initial response is intentionally only the starting point. Zepto
        # lazy-loads additional PRODUCT_GRID results as the real page scrolls.
        # Repeatedly move to the current bottom, wait for the loader/network,
        # then measure whether the feed grew or new search responses arrived.
        max_scroll_rounds = 80
        stable_rounds = 0
        last_height = -1
        last_response_count = len(search_responses)
        last_product_count = 0

        for scroll_round in range(max_scroll_rounds):
            # Give any response handler tasks a chance to finish before making
            # the next scroll decision.
            await page.wait_for_timeout(300)

            try:
                product_count = sum(
                    len(extract_products(item.get("body")))
                    for item in search_responses
                    if isinstance(item, dict)
                )
            except Exception:
                product_count = 0

            try:
                metrics = await page.evaluate(
                    """
                    () => {
                      const doc = document.documentElement;
                      const body = document.body;
                      const height = Math.max(
                        doc ? doc.scrollHeight : 0,
                        body ? body.scrollHeight : 0
                      );
                      const viewport = window.innerHeight || 0;
                      const y = window.scrollY || 0;
                      return {
                        height,
                        viewport,
                        y,
                        bottom: y + viewport,
                      };
                    }
                    """
                )
                height = int(metrics.get("height") or 0)
            except Exception:
                height = 0

            # Scroll close to the current bottom. New products can increase the
            # document height, so the next iteration will scroll to the new
            # bottom again.
            try:
                await page.evaluate(
                    """
                    () => {
                      window.scrollTo({
                        top: Math.max(
                          document.documentElement.scrollHeight,
                          document.body ? document.body.scrollHeight : 0
                        ),
                        behavior: 'instant'
                      });
                    }
                    """
                )
            except Exception:
                try:
                    await page.evaluate(
                        "window.scrollTo(0, document.documentElement.scrollHeight)"
                    )
                except Exception:
                    pass

            await page.wait_for_timeout(1500)

            current_response_count = len(search_responses)
            try:
                current_product_count = sum(
                    len(extract_products(item.get("body")))
                    for item in search_responses
                    if isinstance(item, dict)
                )
            except Exception:
                current_product_count = 0

            try:
                current_metrics = await page.evaluate(
                    """
                    () => ({
                      height: Math.max(
                        document.documentElement.scrollHeight || 0,
                        document.body ? document.body.scrollHeight : 0
                      ),
                      y: window.scrollY || 0,
                      viewport: window.innerHeight || 0
                    })
                    """
                )
                current_height = int(current_metrics.get("height") or 0)
            except Exception:
                current_height = height

            logger.info(
                "[Zepto] Scroll round=%d responses=%d products=%d documentHeight=%d stableRounds=%d",
                scroll_round + 1,
                current_response_count,
                current_product_count,
                current_height,
                stable_rounds,
            )

            # Check the latest response for an explicit end condition.
            latest_body = search_responses[-1].get("body") if search_responses else None
            latest_total = 0
            latest_end = False
            if isinstance(latest_body, dict):
                try:
                    latest_total = int(latest_body.get("totalProductCount") or 0)
                except (TypeError, ValueError):
                    latest_total = 0
                latest_end = latest_body.get("hasReachedEnd") is True

            if latest_end:
                logger.info("[Zepto] Scroll loader reported hasReachedEnd=true.")
                break

            if latest_total > 0 and current_product_count >= latest_total:
                logger.info(
                    "[Zepto] Scroll loader reached totalProductCount=%d.",
                    latest_total,
                )
                break

            changed = (
                current_response_count != last_response_count
                or current_product_count != last_product_count
                or current_height > last_height
            )

            if changed:
                stable_rounds = 0
            else:
                stable_rounds += 1

            last_response_count = current_response_count
            last_product_count = current_product_count
            last_height = current_height

            # Three consecutive bottom checks with no new response/products or
            # page growth means the lazy loader has stopped responding.
            if stable_rounds >= 3:
                logger.info(
                    "[Zepto] Scroll loader stabilized; stopping after %d rounds.",
                    scroll_round + 1,
                )
                break

        # Return every search response generated by the browser while scrolling.
        # extract_products() accepts a list and performs cross-page deduplication.
        page_bodies = [
            item["body"]
            for item in search_responses
            if isinstance(item, dict) and isinstance(item.get("body"), dict)
        ]

        if not page_bodies:
            raise RuntimeError("Zepto returned no search result bodies.")

        try:
            final_products = extract_products(page_bodies)
            logger.info(
                "[Zepto] Scroll search complete: responses=%d uniqueProducts=%d",
                len(page_bodies),
                len(final_products),
            )
        except Exception:
            pass

        return page_bodies if len(page_bodies) > 1 else page_bodies[0]

    finally:
        with suppress(Exception):
            page.remove_listener("response", response_handler)


# ---------------------------------------------------------------------------
# Main browser fetch
# ---------------------------------------------------------------------------

async def _fetch_zepto_products_browser(
    query: str,
    lat: float,
    lon: float,
    expected_store_id: str = "",
) -> List[Dict[str, Any]]:
    # One shared page is intentionally serialized. This avoids launching a
    # new Chromium process and repeating Zepto location/serviceability setup
    # for every comparison request.
    async with _ZEPTO_PERSISTENT_LOCK:
        try:
            _, _, context, page, live_store_id = await _get_persistent_zepto_session(
                lat,
                lon,
            )

            if context is None or page is None:
                logger.info("[Zepto] Browser session could not be initialized.")
                return []

            if expected_store_id and live_store_id and expected_store_id != live_store_id:
                logger.info(
                    "[Zepto] Ignoring caller store ID; live Zepto store is authoritative."
                )

            serviceability_state = get_zepto_serviceability_state()
            if serviceability_state.get("status") == "unserviceable":
                logger.info(
                    "[Zepto] Skipping product search because location is confirmed unserviceable."
                )
                return []

            body = await _perform_browser_search(
                page,
                query,
                context=context,
                latitude=lat,
                longitude=lon,
                store_id=live_store_id,
            )

            products = extract_products(
                body,
                store_id=live_store_id,
            )

            logger.info(
                "[Zepto] Browser search returned %d products.",
                len(products),
            )

            return products

        except PlaywrightTimeoutError as exc:
            logger.warning("[Zepto] Browser timeout: %s", exc)
            return []

        except Exception as exc:
            logger.warning(
                "[Zepto] Browser product fetch failed: %s: %s",
                type(exc).__name__,
                exc,
            )

            with suppress(Exception):
                await _close_persistent_zepto_session()

            return []


async def fetch_zepto_products(
    query: str,
    lat: float,
    lon: float,
    store_id: str = "",
    cookie: str = "",
    *args,
    **kwargs,
) -> List[Dict[str, Any]]:
    """
    Public Zepto product fetcher.

    `cookie` and arbitrary legacy kwargs are accepted for compatibility, but
    browser authentication/session state is created and used locally by the
    Playwright browser.
    """
    try:
        f_lat = float(lat)
        f_lon = float(lon)
    except (TypeError, ValueError):
        logger.warning("[Zepto] Invalid coordinates.")
        return []

    if f_lat == 0.0 and f_lon == 0.0:
        logger.warning("[Zepto] Missing coordinates.")
        return []

    query = (query or "").strip()
    if not query:
        return []

    return await _fetch_zepto_products_browser(
        query=query,
        lat=f_lat,
        lon=f_lon,
        expected_store_id=str(store_id or "").strip(),
    )


fetch_zepto_prices = fetch_zepto_products


# ---------------------------------------------------------------------------
# Compatibility endpoints
# ---------------------------------------------------------------------------

@app.post("/api/request-otp")
async def request_zepto_otp(payload: AuthPhoneRequest):
    return {
        "status": "success",
        "message": "Verification code dispatched successfully.",
    }


@app.post("/api/validate-otp")
async def validate_zepto_otp(payload: AuthOtpRequest):
    return {
        "status": "success",
        "session_ref_key": "mock_session_key",
    }


@app.post("/api/get-prices")
async def get_prices(payload: PriceRequest, **kwargs):
    query = (payload.query or "").strip()

    if not query:
        return {
            "query": query,
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "store_id": payload.store_id or "",
            "pincode": payload.pincode or "",
            "products": [],
            "count": 0,
        }

    products = await fetch_zepto_products(
        query,
        payload.latitude,
        payload.longitude,
        store_id=payload.store_id or "",
        cookie="",
        session_ref_key=payload.session_ref_key,
    )

    live_store_id = ""
    if products:
        live_store_id = str(products[0].get("store_id") or "")

    return {
        "query": query,
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "store_id": live_store_id or payload.store_id or "",
        "pincode": payload.pincode or "",
        "products": products,
        "count": len(products),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8003)
