import asyncio
from playwright.async_api import async_playwright

LAT = 21.1458
LON = 79.0882
URL = "https://blinkit.com/"

GEO_SCRIPT = f"""
(() => {{
    const latitude = {LAT!r};
    const longitude = {LON!r};
    const accuracy = 10;

    const position = {{
        coords: {{
            latitude,
            longitude,
            accuracy,
            altitude: null,
            altitudeAccuracy: null,
            heading: null,
            speed: null
        }},
        timestamp: Date.now()
    }};

    const geolocation = {{
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
            configurable: true,
            get: () => geolocation
        }});
    }} catch (e) {{
        try {{
            Object.defineProperty(navigator, "geolocation", {{
                configurable: true,
                get: () => geolocation
            }});
        }} catch (ignored) {{}}
    }}
}})();
"""

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1365, "height": 900},
            geolocation={"latitude": LAT, "longitude": LON, "accuracy": 10},
            permissions=["geolocation"],
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        await context.add_init_script(GEO_SCRIPT)
        page = await context.new_page()

        print("Opening Blinkit...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        print("\n--- GEOLOCATION TEST ---")
        geo = await page.evaluate("""
        () => new Promise(resolve => {
            navigator.geolocation.getCurrentPosition(
                p => resolve({
                    success: true,
                    latitude: p.coords.latitude,
                    longitude: p.coords.longitude,
                    accuracy: p.coords.accuracy
                }),
                e => resolve({
                    success: false,
                    code: e.code,
                    message: e.message
                }),
                {enableHighAccuracy: true, timeout: 10000}
            );
        })
        """)
        print(geo)

        print("\n--- BEFORE CLICK ---")
        print("Detect button:", await page.locator(
            'button.location-box.mask-button',
            has_text="Detect my location"
        ).count())
        print("Location input:", await page.locator(
            'input[name="select-locality"]'
        ).count())

        detect = page.locator(
            'button.location-box.mask-button',
            has_text="Detect my location"
        ).first

        if await detect.is_visible():
            print("\nClicking Detect my location...")
            await detect.click()
            await page.wait_for_timeout(5000)
        else:
            print("\nDetect button is not visible; stopping diagnostic.")

        print("\n--- AFTER CLICK ---")
        print("Detect visible:",
              await detect.is_visible() if await detect.count() else False)
        print("Location input:",
              await page.locator('input[name="select-locality"]').count())

        inputs = await page.locator("input").all()
        print("\nInputs currently on page:")
        for i, inp in enumerate(inputs):
            try:
                print(
                    f"[{i}] placeholder={await inp.get_attribute('placeholder')!r} "
                    f"name={await inp.get_attribute('name')!r} "
                    f"type={await inp.get_attribute('type')!r}"
                )
            except Exception:
                pass

        print("\nCurrent URL:", page.url)
        print("\nLeave the browser open for inspection. Press Ctrl+C to exit.")

        while True:
            await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
