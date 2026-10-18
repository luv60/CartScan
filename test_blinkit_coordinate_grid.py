import asyncio
from playwright.async_api import async_playwright

CENTER_LAT = 21.1458
CENTER_LON = 79.0882

# Rough grid around the user's coordinate.
# ~0.001 degrees is about 111 m latitude / ~105 m longitude here.
OFFSETS = [
    (0.0000, 0.0000),
    (0.0010, 0.0000),
    (-0.0010, 0.0000),
    (0.0000, 0.0010),
    (0.0000, -0.0010),
    (0.0020, 0.0000),
    (-0.0020, 0.0000),
    (0.0000, 0.0020),
    (0.0000, -0.0020),
    (0.0030, 0.0000),
    (-0.0030, 0.0000),
    (0.0000, 0.0030),
    (0.0000, -0.0030),
]

def geo_script(lat, lon):
    return f"""
(() => {{
    const position = {{
        coords: {{
            latitude: {lat!r}, longitude: {lon!r}, accuracy: 10,
            altitude: null, altitudeAccuracy: null,
            heading: null, speed: null
        }},
        timestamp: Date.now()
    }};
    const geo = {{
        getCurrentPosition(success, error, options) {{
            if (typeof success === "function") success(position);
        }},
        watchPosition(success, error, options) {{
            if (typeof success === "function") success(position);
            return 1;
        }},
        clearWatch(id) {{}}
    }};
    try {{
        Object.defineProperty(Navigator.prototype, "geolocation", {{
            configurable: true, get: () => geo
        }});
    }} catch (e) {{}}
}})();
"""

async def test(browser, lat, lon):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        geolocation={"latitude": lat, "longitude": lon, "accuracy": 10},
        permissions=["geolocation"],
        locale="en-IN",
        timezone_id="Asia/Kolkata",
    )
    await context.add_init_script(geo_script(lat, lon))
    page = await context.new_page()

    try:
        await page.goto("https://blinkit.com/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)

        button = page.locator(
            'button.location-box.mask-button',
            has_text="Detect my location"
        ).first

        if await button.count() == 0 or not await button.is_visible():
            return "NO_BUTTON"

        await button.click()
        await page.wait_for_timeout(4000)

        still_open = await page.locator(
            'input[name="select-locality"]'
        ).count() > 0

        return "REJECTED" if still_open else "ACCEPTED"
    except Exception as e:
        return f"ERROR:{type(e).__name__}"
    finally:
        await context.close()

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)

        print("=" * 78)
        print("BLINKIT COORDINATE SERVICEABILITY GRID")
        print("=" * 78)
        print(f"Center: {CENTER_LAT}, {CENTER_LON}")
        print()
        print("lat,lon,status")
        print("-" * 78)

        results = []
        for dlat, dlon in OFFSETS:
            lat = CENTER_LAT + dlat
            lon = CENTER_LON + dlon
            status = await test(browser, lat, lon)
            results.append((lat, lon, status))
            print(f"{lat:.6f},{lon:.6f},{status}")

        print()
        print("=" * 78)
        print("SUMMARY")
        print("=" * 78)

        accepted = [x for x in results if x[2] == "ACCEPTED"]
        rejected = [x for x in results if x[2] == "REJECTED"]

        print("Accepted:", len(accepted))
        print("Rejected:", len(rejected))

        if accepted:
            print("\nAccepted coordinates:")
            for lat, lon, status in accepted:
                print(f"  {lat:.6f}, {lon:.6f}")

        if not accepted:
            print("\nNo nearby coordinate was accepted.")
            print("This suggests the issue is not a tiny coordinate boundary.")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
