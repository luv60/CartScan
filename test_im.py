import json
import urllib.parse
from curl_cffi import requests

session = requests.Session(impersonate="chrome124")

# User's local test coordinates
lat, lon = "21.1458", "79.0882"
q = urllib.parse.quote("milk")

init_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
session.get(f"https://www.swiggy.com/instamart?lat={lat}&lng={lon}", headers=init_headers, timeout=10)

search_url = f"https://www.swiggy.com/dapi/restaurants/search/v3?lat={lat}&lng={lon}&str={q}&trackingId=undefined&submitAction=ENTER&queryUniqueId=quickcompare_im&selectedPLTab=INSTAMART"

api_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.swiggy.com/instamart",
    "Origin": "https://www.swiggy.com",
    "device-type": "web"
}

r = session.get(search_url, headers=api_headers, timeout=10)
print("Status:", r.status_code)

data = r.json().get("data", {})
cards = data.get("cards", [])

# Let's inspect the exact card types Swiggy returned
for idx, c in enumerate(cards):
    card_type = c.get("card", {}).get("card", {}).get("@type", "No @type")
    print(f"Card [{idx}] Type: {card_type}")

# Check whether Swiggy has an explicit 'INSTAMART' tab or groupedCard in this response
grouped = data.get("groupedCard", {}).get("cardGroupMap", {})
print("Available group tabs:", list(grouped.keys()))