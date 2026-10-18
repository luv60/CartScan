"""
blinkit_service.py - Standalone Async HTTP Blinkit Scraper Service

Fetches product payloads directly from Blinkit layout search API using curl_cffi AsyncSession.
Resolves Blinkit serviceability/normalized coordinates before searching.
"""

import asyncio
import logging
import random
import re
import urllib.parse
from typing import List, Dict, Any, Optional

from curl_cffi.requests import AsyncSession
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("blinkit_service")


# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------

app = FastAPI(
    title="Blinkit Async Scraper Service",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# Quantity helpers
# -----------------------------------------------------------------------------

def extract_quantity_in_base_units(text: str):
    """Returns (amount, 'g' or 'ml') converted to base grams or milliliters."""

    if not text:
        return 1.0, "unit"

    t = text.lower()

    # Liters
    m = re.search(
        r'(\d+(?:\.\d+)?)\s*(?:l|ltr|litre|litres|liter)\b',
        t
    )

    if m:
        return float(m.group(1)) * 1000.0, "ml"

    # Milliliters
    m = re.search(
        r'(\d+(?:\.\d+)?)\s*(?:ml|milli)\b',
        t
    )

    if m:
        return float(m.group(1)), "ml"

    # Kilograms
    m = re.search(
        r'(\d+(?:\.\d+)?)\s*(?:kg|kilo|kilogram)\b',
        t
    )

    if m:
        return float(m.group(1)) * 1000.0, "g"

    # Grams
    m = re.search(
        r'(\d+(?:\.\d+)?)\s*(?:g|gm|grams?)\b',
        t
    )

    if m:
        return float(m.group(1)), "g"

    return 1.0, "unit"


def extract_packsize_from_title(
    title: str,
    current_pack: str = ""
) -> str:

    current = (current_pack or "").strip()
    c_lower = current.lower()

    if not current or c_lower in [
        "1 pack",
        "1 pc",
        "1 piece",
        "pack",
        "unit",
        "1 unit",
        ""
    ]:

        m = re.search(
            r'\b(\d+(?:\.\d+)?)\s*'
            r'(ml|l|ltr|litre|litres|liter|kg|kilo|g|gm|grams?)\b',
            title,
            re.IGNORECASE
        )

        if m:
            val = m.group(1)
            u = m.group(2).lower()

            if u in ["l", "ltr", "litre", "litres", "liter"]:
                return f"{val} L"

            elif u in ["ml"]:
                return f"{val} ml"

            elif u in ["kg", "kilo"]:
                return f"{val} kg"

            elif u in ["g", "gm", "gram", "grams"]:
                return f"{val} g"

            return f"{val} {u}"

    return current or "1 pack"


def calculate_unit_price(
    name: str,
    packsize: str,
    price: float
) -> Dict[str, Any]:

    if price is None or price <= 0:
        return {
            "normalized_price": 0.0,
            "unit": "per pack",
            "formatted": "₹0.00 / pack"
        }

    combined = f"{name} {packsize}".lower()

    # Example: 2 x 500 g
    mult_match = re.search(
        r'(\d+)\s*[xX*]\s*'
        r'(\d+(?:\.\d+)?)\s*'
        r'(kg|kilo|kilogram|g|gm|gms|gram|l|ltr|litre|liter|ml)\b',
        combined
    )

    if mult_match:

        multiplier = float(mult_match.group(1))

        sub_qty, base_u = extract_quantity_in_base_units(
            f"{mult_match.group(2)} {mult_match.group(3)}"
        )

        total_base = multiplier * sub_qty

        if base_u == "g":
            norm = round((price / total_base) * 100, 2)

            return {
                "normalized_price": norm,
                "unit": "100g",
                "formatted": f"₹{norm:.2f} / 100g"
            }

        elif base_u == "ml":
            norm = round((price / total_base) * 100, 2)

            return {
                "normalized_price": norm,
                "unit": "100ml",
                "formatted": f"₹{norm:.2f} / 100ml"
            }

    base_qty, base_u = extract_quantity_in_base_units(combined)

    if base_u == "g" and base_qty > 0:

        norm = round((price / base_qty) * 100, 2)

        return {
            "normalized_price": norm,
            "unit": "100g",
            "formatted": f"₹{norm:.2f} / 100g"
        }

    elif base_u == "ml" and base_qty > 0:

        norm = round((price / base_qty) * 100, 2)

        return {
            "normalized_price": norm,
            "unit": "100ml",
            "formatted": f"₹{norm:.2f} / 100ml"
        }

    return {
        "normalized_price": round(price, 2),
        "unit": "per pack",
        "formatted": f"₹{price:.2f} / pack"
    }


# -----------------------------------------------------------------------------
# Blinkit location resolution
# -----------------------------------------------------------------------------

async def resolve_blinkit_location(
    latitude: float,
    longitude: float
) -> Optional[Dict[str, float]]:

    """
    Resolve user coordinates through Blinkit's /location/info endpoint.

    Blinkit may normalize the supplied coordinates to the actual serviceable
    coordinate. Search should use the returned normalized coordinate.
    """

    location_url = "https://blinkit.com/location/info"

    location_headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://blinkit.com/",
        "Origin": "https://blinkit.com",
        "app_client": "consumer_web",
        "platform": "desktop_web",
        "device-type": "web",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    location_params = {
        "lat": latitude,
        "lon": longitude,
        "is_pin_moved": "true",
    }

    try:

        async with AsyncSession(
            impersonate="chrome124"
        ) as session:

            response = await session.get(
                location_url,
                params=location_params,
                headers=location_headers,
                timeout=10,
            )

        if response.status_code != 200:

            logger.warning(
                f"Blinkit location API HTTP "
                f"{response.status_code} for "
                f"{latitude}, {longitude}"
            )

            return None

        try:
            data = response.json()

        except Exception as e:

            logger.warning(
                f"Blinkit location API returned invalid JSON: {e}"
            )

            return None

        if not data.get("is_serviceable", False):

            logger.warning(
                f"Blinkit location is NOT serviceable for "
                f"{latitude}, {longitude}"
            )

            return None

        coordinate = data.get("coordinate") or {}

        normalized_lat = coordinate.get("lat")
        normalized_lon = coordinate.get("lon")

        if normalized_lat is None or normalized_lon is None:

            logger.warning(
                "Blinkit location response is serviceable "
                "but contains no normalized coordinates"
            )

            return None

        normalized_lat = float(normalized_lat)
        normalized_lon = float(normalized_lon)

        logger.info(
            f"Blinkit location resolved: "
            f"{normalized_lat}, {normalized_lon}"
        )

        return {
            "latitude": normalized_lat,
            "longitude": normalized_lon,
        }

    except Exception as e:

        logger.warning(
            f"Blinkit location resolution failed: {e}"
        )

        return None


# -----------------------------------------------------------------------------
# Main Blinkit scraper
# -----------------------------------------------------------------------------

async def fetch_blinkit_products(
    query: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    store_id: Optional[str] = None,
    max_retries: int = 3,
    limit: int = 100,
    max_pages: int = 5
) -> List[Dict[str, Any]]:

    """
    Direct async HTTP API fetcher for Blinkit search layout.

    Steps:

    1. Resolve Blinkit serviceable/normalized coordinates.
    2. Search using normalized coordinates.
    3. Paginate through results.
    4. Extract product name, price, MRP, inventory, image, etc.
    5. Return normalized QuickCompare product objects.
    """

    cleaned_q = query.strip()

    if not cleaned_q:
        return []

    # -------------------------------------------------------------------------
    # 1. Get input coordinates
    # -------------------------------------------------------------------------

    f_lat = float(
        latitude
        if latitude is not None
        else (
            lat
            if lat is not None
            else 21.1762
        )
    )

    f_lon = float(
        longitude
        if longitude is not None
        else (
            lon
            if lon is not None
            else 79.0554
        )
    )

    # -------------------------------------------------------------------------
    # 2. Resolve Blinkit serviceability
    # -------------------------------------------------------------------------

    resolved_location = await resolve_blinkit_location(
        f_lat,
        f_lon
    )

    if not resolved_location:

        logger.warning(
            f"Blinkit could not resolve a serviceable location "
            f"for {f_lat}, {f_lon}"
        )

        return []

    f_lat = resolved_location["latitude"]
    f_lon = resolved_location["longitude"]

    # -------------------------------------------------------------------------
    # 3. Build search URL
    # -------------------------------------------------------------------------

    encoded_q = urllib.parse.quote(cleaned_q)

    base_url = (
        "https://blinkit.com/v1/layout/search"
        f"?q={encoded_q}"
        f"&lat={f_lat}"
        f"&lon={f_lon}"
    )

    # -------------------------------------------------------------------------
    # 4. Search headers
    # -------------------------------------------------------------------------

    search_headers = {
        "Accept": "application/json, text/plain, */*",
        "lat": str(f_lat),
        "lon": str(f_lon),
        "latitude": str(f_lat),
        "longitude": str(f_lon),
        "device-type": "web",
        "Referer": "https://blinkit.com/",
        "Origin": "https://blinkit.com",
        "app_client": "consumer_web",
        "platform": "desktop_web",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    products: List[Dict[str, Any]] = []
    seen_keys = set()

    # -------------------------------------------------------------------------
    # 5. Retry loop
    # -------------------------------------------------------------------------

    for attempt in range(max_retries + 1):

        try:

            async with AsyncSession(
                impersonate="chrome124"
            ) as session:

                curr_url = base_url

                body = {
                    "applied_filters": None,
                    "sort": "",
                    "postback_meta": {},
                    "previous_search_query": "",
                    "vertical_cards_processed": 0,
                }

                offset = 0
                page_count = 0

                # -----------------------------------------------------------------
                # 6. Pagination loop
                # -----------------------------------------------------------------

                while (
                    len(products) < limit
                    and page_count < max_pages
                ):

                    page_count += 1

                    try:

                        response = await session.post(
                            curr_url,
                            headers=search_headers,
                            json=body,
                            timeout=10,
                        )

                        # ---------------------------------------------------------
                        # Retryable errors
                        # ---------------------------------------------------------

                        if response.status_code in (429, 403):

                            delay = (
                                0.5 * (2 ** attempt)
                                + random.uniform(0.1, 0.3)
                            )

                            logger.warning(
                                f"Blinkit API HTTP "
                                f"{response.status_code} "
                                f"on attempt "
                                f"{attempt + 1}/{max_retries + 1} "
                                f"for query '{cleaned_q}', "
                                f"retrying in {delay:.2f}s"
                            )

                            await asyncio.sleep(delay)

                            break

                        # ---------------------------------------------------------
                        # Other HTTP errors
                        # ---------------------------------------------------------

                        if response.status_code != 200:

                            logger.warning(
                                f"Blinkit API HTTP "
                                f"{response.status_code} "
                                f"for query '{cleaned_q}'"
                            )

                            break

                        # ---------------------------------------------------------
                        # Parse response
                        # ---------------------------------------------------------

                        data = response.json()

                        resp_obj = data.get(
                            "response",
                            {}
                        )

                        snippets = resp_obj.get(
                            "snippets",
                            []
                        )

                        if not snippets:
                            break

                        batch_added = 0

                        # ---------------------------------------------------------
                        # Product extraction
                        # ---------------------------------------------------------

                        for snippet in snippets:

                            sd = snippet.get(
                                "data",
                                {}
                            )

                            cart_item = (
                                sd
                                .get("atc_action", {})
                                .get("add_to_cart", {})
                                .get("cart_item", {})
                            )

                            # -----------------------------------------------------
                            # Product name
                            # -----------------------------------------------------

                            name = (
                                cart_item.get("product_name")
                                or cart_item.get("display_name")
                                or ""
                            )

                            if (
                                not name
                                and isinstance(
                                    sd.get("name"),
                                    dict
                                )
                            ):

                                name = sd["name"].get(
                                    "text",
                                    ""
                                )

                            elif (
                                not name
                                and isinstance(
                                    sd.get("name"),
                                    str
                                )
                            ):

                                name = sd["name"]

                            # -----------------------------------------------------
                            # Price
                            # -----------------------------------------------------

                            raw_selling_price = (
                                cart_item.get("price")
                                or sd.get("normal_price")
                                or sd.get("price")
                                or 0
                            )

                            raw_mrp = (
                                cart_item.get("mrp")
                                or sd.get("mrp")
                                or sd.get("strike_through_price")
                                or 0
                            )

                            price = 0.0

                            if isinstance(
                                raw_selling_price,
                                dict
                            ):

                                price_text = (
                                    raw_selling_price.get(
                                        "text",
                                        ""
                                    )
                                )

                                number = re.sub(
                                    r"[^\d.]",
                                    "",
                                    price_text
                                )

                                if number:
                                    price = float(number)

                            elif isinstance(
                                raw_selling_price,
                                (int, float, str)
                            ):

                                number = re.sub(
                                    r"[^\d.]",
                                    "",
                                    str(raw_selling_price)
                                )

                                if number:
                                    price = float(number)

                            # -----------------------------------------------------
                            # MRP
                            # -----------------------------------------------------

                            mrp = 0.0

                            if isinstance(
                                raw_mrp,
                                dict
                            ):

                                mrp_text = (
                                    raw_mrp.get(
                                        "text",
                                        ""
                                    )
                                )

                                number = re.sub(
                                    r"[^\d.]",
                                    "",
                                    mrp_text
                                )

                                if number:
                                    mrp = float(number)

                            elif isinstance(
                                raw_mrp,
                                (int, float, str)
                            ):

                                number = re.sub(
                                    r"[^\d.]",
                                    "",
                                    str(raw_mrp)
                                )

                                if number:
                                    mrp = float(number)

                            # -----------------------------------------------------
                            # Handle paise representations
                            # -----------------------------------------------------

                            if (
                                price > 5000
                                and price % 100 == 0
                            ):

                                price /= 100.0

                            if (
                                mrp > 5000
                                and mrp % 100 == 0
                            ):

                                mrp /= 100.0

                            if (
                                mrp <= 0.0
                                and price > 0.0
                            ):

                                mrp = price

                            # -----------------------------------------------------
                            # Unit / variant
                            # -----------------------------------------------------

                            unit = (
                                cart_item.get("unit")
                                or ""
                            )

                            if (
                                not unit
                                and isinstance(
                                    sd.get("variant"),
                                    dict
                                )
                            ):

                                unit = sd["variant"].get(
                                    "text",
                                    ""
                                )

                            elif (
                                not unit
                                and isinstance(
                                    sd.get("variant"),
                                    str
                                )
                            ):

                                unit = sd["variant"]

                            # -----------------------------------------------------
                            # Image extraction
                            # -----------------------------------------------------

                            image_url = ""

                            product_data = (
                                sd.get("product")
                                if isinstance(
                                    sd.get("product"),
                                    dict
                                )
                                else {}
                            )

                            product_info = (
                                sd.get("product_info")
                                if isinstance(
                                    sd.get("product_info"),
                                    dict
                                )
                                else {}
                            )

                            candidates = [
                                sd,
                                product_data,
                                product_info,
                                cart_item,
                            ]

                            for candidate in candidates:

                                if not isinstance(
                                    candidate,
                                    dict
                                ):
                                    continue

                                candidate_image_url = (
                                    candidate.get(
                                        "image_url"
                                    )
                                )

                                if (
                                    candidate_image_url
                                    and isinstance(
                                        candidate_image_url,
                                        str
                                    )
                                ):

                                    image_url = (
                                        candidate_image_url
                                    )

                                    break

                                candidate_image = (
                                    candidate.get("image")
                                )

                                if isinstance(
                                    candidate_image,
                                    dict
                                ):

                                    image_url = (
                                        candidate_image.get(
                                            "url"
                                        )
                                        or candidate_image.get(
                                            "image_url"
                                        )
                                        or ""
                                    )

                                    if image_url:
                                        break

                                elif isinstance(
                                    candidate_image,
                                    str
                                ):

                                    image_url = (
                                        candidate_image
                                    )

                                    break

                            # -----------------------------------------------------
                            # Normalize image URL
                            # -----------------------------------------------------

                            if (
                                image_url
                                and not image_url.startswith(
                                    "http"
                                )
                            ):

                                if image_url.startswith("//"):

                                    image_url = (
                                        "https:"
                                        + image_url
                                    )

                                elif image_url.startswith("/"):

                                    image_url = (
                                        "https://cdn.grofers.com"
                                        + image_url
                                    )

                                else:

                                    image_url = (
                                        "https://cdn.grofers.com/"
                                        + image_url
                                    )

                            # -----------------------------------------------------
                            # Product ID
                            # -----------------------------------------------------

                            identity = sd.get(
                                "identity",
                                {}
                            )

                            product_id = (
                                cart_item.get("product_id")
                                or sd.get("product_id")
                                or cart_item.get("id")
                                or identity.get("id")
                            )

                            # -----------------------------------------------------
                            # Buy URL
                            # -----------------------------------------------------

                            if product_id:

                                buy_url = (
                                    "https://blinkit.com/prn/x/prid/"
                                    f"{product_id}"
                                )

                            else:

                                buy_url = (
                                    "https://blinkit.com/s/"
                                    f"?q={encoded_q}"
                                )

                            # -----------------------------------------------------
                            # Inventory / OOS
                            # -----------------------------------------------------

                            inventory = (
                                cart_item.get("inventory")
                                or sd.get("inventory")
                            )

                            out_of_stock = False

                            if inventory is not None:

                                if (
                                    isinstance(
                                        inventory,
                                        (int, float)
                                    )
                                    and inventory <= 0
                                ):

                                    out_of_stock = True

                                elif isinstance(
                                    inventory,
                                    dict
                                ):

                                    if (
                                        inventory.get(
                                            "is_out_of_stock"
                                        ) is True
                                        or inventory.get(
                                            "quantity",
                                            1
                                        ) <= 0
                                    ):

                                        out_of_stock = True

                            if (
                                sd.get(
                                    "is_out_of_stock"
                                ) is True
                                or cart_item.get(
                                    "is_out_of_stock"
                                ) is True
                            ):

                                out_of_stock = True

                            button_text = str(
                                sd.get(
                                    "atc_action",
                                    {}
                                )
                            ).lower()

                            if (
                                "notify" in button_text
                                or "out of stock"
                                in button_text
                            ):

                                out_of_stock = True

                            # -----------------------------------------------------
                            # Deduplication
                            # -----------------------------------------------------

                            clean_key = (
                                f"{name.strip().lower()}_"
                                f"{unit.strip().lower()}"
                            )

                            if (
                                name
                                and price >= 5
                                and clean_key not in seen_keys
                            ):

                                seen_keys.add(
                                    clean_key
                                )

                                clean_pack = (
                                    extract_packsize_from_title(
                                        name,
                                        unit or "1 pack"
                                    )
                                )

                                unit_calc = (
                                    calculate_unit_price(
                                        name,
                                        clean_pack,
                                        price
                                    )
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
                                        "normalized_price": unit_calc[
                                            "normalized_price"
                                        ],
                                        "unit": unit_calc[
                                            "unit"
                                        ],
                                        "formatted_unit_price": unit_calc[
                                            "formatted"
                                        ],
                                        "image_url": image_url,
                                        "buy_url": buy_url,
                                        "product_id": (
                                            str(product_id)
                                            if product_id
                                            else ""
                                        ),
                                        "store": "Blinkit",
                                    }
                                )

                                batch_added += 1

                        # ---------------------------------------------------------
                        # Pagination
                        # ---------------------------------------------------------

                        pagination = resp_obj.get(
                            "pagination",
                            {}
                        )

                        is_end_of_list = pagination.get(
                            "end_of_list",
                            False
                        )

                        next_url = pagination.get(
                            "next_url"
                        )

                        if (
                            is_end_of_list
                            or not next_url
                            or batch_added == 0
                        ):

                            break

                        offset += len(snippets)

                        curr_url = (
                            f"https://blinkit.com{next_url}"
                        )

                        if "lat=" not in curr_url:

                            separator = (
                                "&"
                                if "?" in curr_url
                                else "?"
                            )

                            curr_url = (
                                f"{curr_url}"
                                f"{separator}"
                                f"lat={f_lat}"
                                f"&lon={f_lon}"
                            )

                        next_body = data.get(
                            "postback_params"
                        )

                        if isinstance(
                            next_body,
                            dict
                        ):

                            body = next_body.copy()

                        body["vertical_cards_processed"] = offset

                    except Exception as loop_error:

                        logger.error(
                            f"Blinkit pagination loop "
                            f"error on page "
                            f"{page_count}: "
                            f"{loop_error}"
                        )

                        break

            if products:
                break

        except Exception as error:

            logger.warning(
                f"Blinkit fetch attempt "
                f"{attempt + 1} failed: "
                f"{error}"
            )

            if attempt < max_retries:

                delay = (
                    0.5 * (2 ** attempt)
                )

                await asyncio.sleep(delay)

    logger.info(
        f"Blinkit async fetcher returned "
        f"{len(products)} products across pages "
        f"for '{cleaned_q}'"
    )

    return products


# -----------------------------------------------------------------------------
# Alias used by server.py
# -----------------------------------------------------------------------------

fetch_blinkit = fetch_blinkit_products


# -----------------------------------------------------------------------------
# API models
# -----------------------------------------------------------------------------

class BlinkitPriceRequest(BaseModel):

    latitude: Optional[float] = None
    longitude: Optional[float] = None

    lat: Optional[float] = None
    lon: Optional[float] = None

    query: str

    store_id: Optional[str] = None


# -----------------------------------------------------------------------------
# API endpoint
# -----------------------------------------------------------------------------

@app.post("/api/get-prices")
async def get_prices(
    payload: BlinkitPriceRequest
):

    request_lat = (
        payload.latitude
        if payload.latitude is not None
        else payload.lat
    )

    request_lon = (
        payload.longitude
        if payload.longitude is not None
        else payload.lon
    )

    items = await fetch_blinkit_products(
        query=payload.query,
        latitude=request_lat,
        longitude=request_lon,
        store_id=payload.store_id,
    )

    return {
        "query": payload.query,
        "latitude": request_lat,
        "longitude": request_lon,
        "count": len(items),
        "products": items,
    }


# -----------------------------------------------------------------------------
# Health endpoint
# -----------------------------------------------------------------------------

@app.get("/health")
def health():

    return {
        "status": "ok",
        "service": "blinkit-async-scraper"
    }


# -----------------------------------------------------------------------------
# Standalone execution
# -----------------------------------------------------------------------------

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "blinkit_service:app",
        host="0.0.0.0",
        port=8002,
        reload=True
    )