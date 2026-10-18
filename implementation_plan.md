# Implementation Plan — Pure WebView-First Zepto Retrieval

## Architecture Overview

```
NATIVE GPS
  ↓
ZEPTO WEBVIEW
  ↓
REAL ZEPTO LOCATION / SERVICEABILITY
  ↓
REAL ZEPTO SEARCH
  ↓
REAL ZEPTO PRODUCT EXTRACTION
  ↓
zepto_products
  ↓
POST /api/compare
  ↓
COMPARISON UI
```

---

## Explicit Architectural Principles & Invariants

1. **main.dart as PRIMARY Zepto Implementation**: The Android WebView inside `main.dart` is the sole source of truth for Zepto's live browser session, WAF clearance, location/serviceability, and product search.
2. **_fetchZeptoClientSide() Implemented**: The currently stubbed `_fetchZeptoClientSide()` in `main.dart` will be fully implemented to execute real searches inside the live Zepto WebView and extract real products.
3. **Real Zepto WebView Search**: Searches execute inside the real WebView by navigating to `https://www.zepto.com/search?q=<query>` within the established browser session.
4. **Real Product Extraction**: Actual Zepto product names, prices (rupee-normalized, never double-divided), pack sizes, IDs, images, and availability are extracted from the WebView.
5. **Payload Handoff to /api/compare**: Extracted products are passed directly as `zepto_products` in `POST /api/compare`.
6. **NO DARK_STORE_MAP Fallback**: `DARK_STORE_MAP` is completely excluded from the live pricing and comparison path.
7. **NO Hardcoded/Default Zepto Store ID**: No static UUIDs, no default store fallbacks under any circumstances.
8. **NO Hardcoded/Default Pincode**: No synthetic or fallback pincodes under any circumstances.
9. **NO Wrong-City Fallback**: If an address/store is unserviceable, return Zepto as unserviceable/unavailable (`zepto_blocked = true`). Never substitute another city or store.
10. **NO Modification to test_matcher.py**: `test_matcher.py` is preserved strictly as-is.
11. **NO Canonical SKU / Cache Redesign**: Canonical SKU matching and cache architecture are not modified.
12. **NO Cookies / WAF / Auth Secrets Sent to Backend**: The backend receives only the extracted public product data.
13. **Strict Unavailable State on Failure**: If Zepto location, serviceability, or search fails, return Zepto unavailable (`zepto_blocked = true`, `zepto_error = "..."`) instead of fabricated or wrong-city data.
14. **Proof of Real Product & Real Price**: Final verification must prove that at least one real Zepto product with a real price traversed: `Zepto WebView` → `extraction` → `zepto_products` → `POST /api/compare` → `comparison result`.

---

## Detailed Component Changes

### Component 1: Flutter Client (`quick_compare_app/lib/main.dart`)

1. **Production Lifecycle Cleanup**:
   - Ensure clean startup of `_zeptoWebViewController` loading `https://www.zepto.com/`.
   - Remove diagnostic observation loops, controlled location click tests, visible DOM location diagnostic overlays, diagnostic reloads, and artificial sleeps.
   - Keep platform geolocation permission prompt callbacks active to allow native browser geolocation.

2. **Live Location via Native GPS**:
   - Obtain user's native GPS via `Geolocator.getCurrentPosition(desiredAccuracy: LocationAccuracy.high)`.
   - Use the existing Android WebView geolocation permission flow so Zepto's browser geolocation can access the device's current location. Do not claim that Dart directly injects arbitrary coordinates into the browser geolocation API unless the implementation actually provides such a mechanism.
   - `serviceability_established=true` may only be reported when an actual Zepto serviceability/location response has been observed and indicates the current session is serviceable/store-bound. Do not infer serviceability merely from WebView page load.
   - Safe logging only:
     - `gps_available`
     - `browser_geolocation_available`
     - `browser_geolocation_success`
     - `serviceability_request_seen`
     - `serviceable`
     - `store_id_present` (boolean flag only; no coordinates or store ID values printed).

3. **Implement `_fetchZeptoClientSide()` & Extraction**:
   - Signature:
     ```dart
     Future<List<Map<String, dynamic>>> _fetchZeptoClientSide(String query, double lat, double lon)
     ```
   - Ensure `_isZeptoReady` is true.
   - Perform search inside the WebView by navigating to `https://www.zepto.com/search?q=${Uri.encodeComponent(query)}`.
   - Poll for search completion (interval 200ms, max 4.5s) to return immediately when products are available.
   - **Extraction Order**:
     - **Strategy A (Intercepted API / `__NEXT_DATA__`)**: If raw subunit prices (paise, e.g. 5800) are encountered, divide by 100 once into rupees (58.0).
     - **Strategy B (Rendered DOM Cards)**: Extract product cards (`[data-testid*="product"]`, `a[href*="/pn/"]`, `[class*="productCard"]`). Read text directly in rupees (`₹58` → 58.0). **NEVER divide already-rupee prices by 100**.
   - Fields extracted per product: `id`, `product_id`, `name`, `price`, `discounted_price`, `mrp`, `packsize`, `image_url`, `buy_url`, `is_available`, `store = "Zepto"`.
   - Deduplicate safely using product ID, or `name + packsize`.

4. **Client-to-Server Handoff**:
   - In `_searchGroceries()`:
     ```dart
     final zeptoProducts = await _fetchZeptoClientSide(query, latVal, lonVal);
     ```
   - Send `POST /api/compare` with:
     ```json
     {
       "query": query,
       "latitude": latVal,
       "longitude": lonVal,
       "zepto_products": zeptoProducts
     }
     ```
   - If `zeptoProducts` is empty, send empty array; the server will return Blinkit and mark Zepto as unavailable.
   - Log the required production test block format:
     ```
     [ZEPTO PRODUCTION TEST]
     webview_ready=...
     browser_geolocation_success=...
     serviceability_established=...
     search_started=true
     zepto_product_count=...
     real_product_data=...
     server_received_zepto_products=true
     blinkit_product_count=...
     comparison_completed=true
     fallback_store_used=false
     fake_data_used=false
     status=PASS
     ```

---

### Component 2: Backend API (`server.py`)

1. **Client-Side Zepto Processing**:
   - In `_do_compare_prices()`:
     - Remove any Zepto store-resolution call that occurs BEFORE the `client_zepto_products` branch. When `client_zepto_products` is supplied, the comparison path must not call `resolve_zepto_store_id()`, `resolve_dark_store_id()`, or `DARK_STORE_MAP` at all.
     - If `client_zepto_products` is non-empty:
       - Use `client_zepto_products` directly.
       - Preserve rupee prices without re-dividing.
       - Do NOT invoke any server-side Zepto fetcher.
     - If `client_zepto_products` is empty or None:
       - **Do NOT call `fetch_zepto_products()`**.
       - **Do NOT use `resolve_zepto_store_id()`, `resolve_dark_store_id()`, or `DARK_STORE_MAP` as a fallback for Zepto**.
       - Set `zepto_blocked = True` and `zepto_error = "No Zepto products returned"` (or "Zepto location/serviceability unavailable").
       - Run Blinkit concurrently and return Blinkit results normally.
2. **Safe Logging**:
   - Do not print cookies, tokens, or exact GPS coordinates.

---

### Component 3: Zepto Service (`zepto_service.py`)

1. **Isolate Stale Fallback Logic**:
   - Keep `extract_products()` as the canonical parser for raw Zepto JSON.
   - Ensure `DARK_STORE_MAP`, hardcoded store IDs, and direct `fetch_zepto_products()` cannot be called by the live comparison route.
   - Preserve existing function signatures required by tests (`test_zepto_service.py`).
   - Remove logging of cookies or tokens.

---

## Verification Plan

### 1. Automated Analysis & Unit Checks
- Run `flutter analyze` in `quick_compare_app/` (0 errors, 0 warnings).
- Run `.\venv\Scripts\python.exe -m pytest test_zepto_service.py` to verify parser functionality.

### 2. Real Android Device End-to-End Test (`R9ZL506DWPW`)
- Set up port forwarding: `adb reverse tcp:8000 tcp:8000`.
- Start local backend: `.\venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000`.
- Run debug application on physical phone `R9ZL506DWPW`.
- Execute query: `milk`.
- Validate that the output log verifies:
  1. `webview_ready=true`
  2. `browser_geolocation_success=true`
  3. `serviceability_established=true`
  4. `zepto_product_count > 0`
  5. `real_product_data=true`
  6. `server_received_zepto_products=true`
  7. `blinkit_product_count > 0`
  8. `comparison_completed=true`
  9. `fallback_store_used=false`
  10. `fake_data_used=false`
  11. `status=PASS`
  12. Verification of at least one real Zepto product name and price reaching the comparison response.
