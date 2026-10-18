import asyncio
from playwright.async_api import async_playwright

COORDS = [
    ("user", 21.1458, 79.0882),
    ("server_default", 21.1718692, 79.0506339),
]

def make_geo_script(lat, lon):
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
    const geolocation = {{
        getCurrentPosition(success, error, options) {{
            console.log("[GEO] getCurrentPosition", JSON.stringify(options || {{}}));
            if (typeof success === "function") success(position);
        }},
        watchPosition(success, error, options) {{
            console.log("[GEO] watchPosition", JSON.stringify(options || {{}}));
            if (typeof success === "function") success(position);
            return 1;
        }},
        clearWatch(id) {{}}
    }};
    try {{
        Object.defineProperty(Navigator.prototype, "geolocation", {{
            configurable: true, get: () => geolocation
        }});
    }} catch (e) {{}}
}})();
"""

async def test(browser, label, lat, lon):
    context = await browser.new_context(
        viewport={"width": 1365, "height": 900},
        geolocation={"latitude": lat, "longitude": lon, "accuracy": 10},
        permissions=["geolocation"],
        locale="en-IN",
        timezone_id="Asia/Kolkata",
    )
    await context.add_init_script(make_geo_script(lat, lon))
    page = await context.new_page()
    page.on("console", lambda msg: print(f"[{label} CONSOLE] {msg.text}"))

    print(f"\n{'='*70}")
    print(f"{label}: {lat}, {lon}")
    print("="*70)

    await page.goto("https://blinkit.com/", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4000)

    geo = await page.evaluate("""
    () => new Promise(resolve => {
        navigator.geolocation.getCurrentPosition(
            p => resolve([p.coords.latitude, p.coords.longitude]),
            e => resolve(["ERROR", e.code, e.message]),
            {timeout: 10000}
        );
    })
    """)
    print("Browser geolocation:", geo)

    button = page.locator(
        'button.location-box.mask-button',
        has_text="Detect my location"
    ).first

    print("Detect visible before:", await button.is_visible())
    await button.click()
    await page.wait_for_timeout(6000)

    print("Detect visible after:", await button.is_visible())
    print("Location input:", await page.locator('input[name="select-locality"]').count())

    body = await page.locator("body").inner_text()
    relevant = [
        x.strip() for x in body.splitlines()
        if x.strip() and any(k in x.lower() for k in [
            "not available", "different location", "delivery in",
            "select location", "please provide"
        ])
    ]
    print("Relevant page text:")
    for x in relevant:
        print(" ", x[:300])

    await context.close()

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        for item in COORDS:
            await test(browser, *item)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
