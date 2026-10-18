from quickcompare_v9_live import V9LiveMatcher

print("=" * 70)
print("QUICKCOMPARE V9 LIVE ADAPTER TEST")
print("=" * 70)

blinkit = {
    "name": "Lay's Classic Salted Potato Chips",
    "brand": "Lay's",
    "quantity": 50,
    "packsize": "50 g",
    "product_id": "test-blinkit-001",
    "price": 20,
    "store": "Blinkit",
}

zepto = {
    "name": "Lays Classic Salted Potato Chips",
    "brand": "Lay's",
    "quantity": 50,
    "packsize": "50 g",
    "product_id": "test-zepto-001",
    "price": 20,
    "store": "Zepto",
}

print("\nInitializing V9 matcher...")

matcher = V9LiveMatcher(
    {
        "blinkit": [blinkit],
        "zepto": [zepto],
        "instamart": [],
    }
)

print("V9 matcher initialized.")

print("\nRunning match...")
result = matcher.score(blinkit, zepto)

print("\n" + "=" * 70)
print("RESULT")
print("=" * 70)
print("Score:", result)
print("=" * 70)