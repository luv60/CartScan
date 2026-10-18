import json
import urllib.parse
from curl_cffi import requests

lat = "21.1458"
lon = "79.0882"
query = "milk"
encoded_q = urllib.parse.quote(query)

print("=" * 60)
print("DIAGNOSTIC SCRAPER: Blinkit & Zepto Endpoints")
print(f"Coordinates: lat={lat}, lon={lon} | Query: '{query}'")
print("=" * 60)

# 1. BLINKIT DIAGNOSTICS
print("\n[1] BLINKIT:")

# 1a. Original endpoint: GET https://blinkit.com/v1/search?q=milk
b_url_v1 = f"https://blinkit.com/v1/search?q={encoded_q}"
b_headers_v1 = {
    "lat": lat,
    "lon": lon,
    "app_client": "consumer_web",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"https://blinkit.com/s/?q={encoded_q}",
    "Origin": "https://blinkit.com"
}
try:
    print(f"\n-> Calling: GET {b_url_v1}")
    r_b1 = requests.get(b_url_v1, headers=b_headers_v1, impersonate="chrome124", timeout=10)
    print("HTTP Status Code:", r_b1.status_code)
    print("Response Headers:", dict(list(r_b1.headers.items())[:6]))
    try:
        b1_json = r_b1.json()
        print("Top-level JSON keys:", list(b1_json.keys()) if isinstance(b1_json, dict) else f"List[{len(b1_json)}]")
    except Exception:
        print("Body is not valid JSON")
    print("First 500 chars of body:\n", r_b1.text[:500])
except Exception as e:
    print("Request failed:", e)

# 1b. Live Layout Endpoint: POST https://blinkit.com/v1/layout/search
print("\n-> Calling Live Layout Endpoint: POST https://blinkit.com/v1/layout/search")
try:
    # Obtain auth_key
    r_auth = requests.get(
        "https://blinkit.com/v2/accounts/auth_key/",
        headers={
            "req_key": "c0e6868e-1180-400c-be51-f473479f1f0a",
            "app_client": "consumer_web",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        impersonate="chrome124",
        timeout=10
    )
    auth_key = r_auth.json().get("auth_key", "")
    print("Obtained Blinkit auth_key:", auth_key[:16] + "...")

    # Test with provided coords
    b_layout_headers = {
        "auth_key": auth_key,
        "app_client": "consumer_web",
        "platform": "desktop_web",
        "lat": lat,
        "lon": lon,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Content-Type": "application/json"
    }
    body = {
        "applied_filters": None,
        "sort": "",
        "postback_meta": {},
        "previous_search_query": "",
        "vertical_cards_processed": 0
    }
    r_b2 = requests.post(
        f"https://blinkit.com/v1/layout/search?q={encoded_q}&search_type=type_to_search",
        headers=b_layout_headers,
        json=body,
        impersonate="chrome124",
        timeout=10
    )
    print("HTTP Status Code (Nagpur coords):", r_b2.status_code)
    print("Response Headers:", dict(list(r_b2.headers.items())[:6]))
    print("Body (Nagpur coords):", r_b2.text[:200])

    # If Nagpur not serviceable, test with active dark store geofence coords
    if r_b2.status_code != 200:
        print("\n-> Testing with active dark store geofence coords (Delhi NCR: 28.6304, 77.2177)...")
        b_layout_headers["lat"] = "28.6304"
        b_layout_headers["lon"] = "77.2177"
        r_b3 = requests.post(
            f"https://blinkit.com/v1/layout/search?q={encoded_q}&search_type=type_to_search",
            headers=b_layout_headers,
            json=body,
            impersonate="chrome124",
            timeout=10
        )
        print("HTTP Status Code (Delhi NCR):", r_b3.status_code)
        print("Response Headers:", dict(list(r_b3.headers.items())[:6]))
        if r_b3.status_code == 200:
            b3_json = r_b3.json()
            print("Top-level JSON keys:", list(b3_json.keys()))
            snippets = b3_json.get("response", {}).get("snippets", [])
            print(f"Total snippets returned: {len(snippets)}")
            print("First 500 chars of body:\n", r_b3.text[:500])
except Exception as e:
    print("Live layout request failed:", e)

# 2. ZEPTO DIAGNOSTICS
print("\n" + "=" * 60)
print("[2] ZEPTO:")

# 2a. Original requested endpoint: https://api.zeptonow.com/api/v1/search/products
z_url = f"https://api.zeptonow.com/api/v1/search/products?query={encoded_q}"
z_headers = {
    "x-user-latitude": lat,
    "x-user-longitude": lon,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"https://www.zeptonow.com/search?query={encoded_q}",
    "Origin": "https://www.zeptonow.com"
}
try:
    print(f"\n-> Calling: GET {z_url}")
    r_z1 = requests.get(z_url, headers=z_headers, impersonate="chrome124", timeout=10)
    print("HTTP Status Code:", r_z1.status_code)
    print("Response Headers:", dict(list(r_z1.headers.items())[:6]))
    try:
        z1_json = r_z1.json()
        print("Top-level JSON keys:", list(z1_json.keys()))
    except Exception:
        print("Body is not valid JSON")
    print("First 500 chars of body:\n", r_z1.text[:500])
except Exception as e:
    print("Request failed (DNS/Network):", e)

# 2b. Web search endpoint: https://www.zeptonow.com/search?query=milk
z_web_url = f"https://www.zeptonow.com/search?query={encoded_q}"
try:
    print(f"\n-> Calling: GET {z_web_url}")
    r_z2 = requests.get(z_web_url, headers=z_headers, impersonate="chrome124", timeout=10)
    print("HTTP Status Code:", r_z2.status_code)
    print("Response Headers:", dict(list(r_z2.headers.items())[:6]))
    print("WAF Action header:", r_z2.headers.get("x-amzn-waf-action"))
    print("First 500 chars of body:\n", r_z2.text[:500])
except Exception as e:
    print("Web search request failed:", e)

print("\n" + "=" * 60)
print("DIAGNOSTIC COMPLETED")
print("=" * 60)
