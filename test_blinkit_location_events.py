import asyncio
from playwright.async_api import async_playwright

LAT = 21.1458
LON = 79.0882

GEO_SCRIPT = f"""
(() => {{
    const latitude = {LAT!r};
    const longitude = {LON!r};
    const accuracy = 10;
    const position = {{
        coords: {{
            latitude, longitude, accuracy,
            altitude: null, altitudeAccuracy: null,
            heading: null, speed: null
        }},
        timestamp: Date.now()
    }};
    const original = navigator.geolocation;
    const geolocation = {{
        getCurrentPosition(success, error, options) {{
            console.log("[GEO-HOOK] getCurrentPosition called", options);
            if (typeof success === "function") success(position);
        }},
        watchPosition(success, error, options) {{
            console.log("[GEO-HOOK] watchPosition called", options);
            if (typeof success === "function") success(position);
            return 1;
        }},
        clearWatch(id) {{
            console.log("[GEO-HOOK] clearWatch called", id);
        }}
    }};
    try {{
        Object.defineProperty(Navigator.prototype, "geolocation", {{
            configurable: true,
            get: () => geolocation
        }});
        console.log("[GEO-HOOK] Navigator.prototype geolocation installed");
    }} catch (e) {{
        console.log("[GEO-HOOK] prototype install failed", String(e));
        try {{
            Object.defineProperty(navigator, "geolocation", {{
                configurable: true,
                get: () => geolocation
            }});
            console.log("[GEO-HOOK] navigator geolocation installed");
        }} catch (e2) {{
            console.log("[GEO-HOOK] navigator install failed", String(e2));
        }}
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

        console_lines = []
        page.on("console", lambda msg: console_lines.append(
            f"[CONSOLE {msg.type}] {msg.text}"
        ))
        page.on("pageerror", lambda exc: console_lines.append(
            f"[PAGEERROR] {exc}"
        ))
        page.on("request", lambda req: (
            console_lines.append(f"[REQUEST] {req.method} {req.url}")
            if any(x in req.url.lower() for x in ["location", "address", "gps", "geo", "delivery"])
            else None
        ))
        page.on("response", lambda resp: (
            console_lines.append(f"[RESPONSE {resp.status}] {resp.url}")
            if any(x in resp.url.lower() for x in ["location", "address", "gps", "geo", "delivery"])
            else None
        ))

        print("Opening Blinkit...")
        await page.goto("https://blinkit.com/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        print("\n--- HOOK CHECK ---")
        hook_check = await page.evaluate("""
        () => ({
            geoExists: !!navigator.geolocation,
            getCurrentPosition: String(navigator.geolocation.getCurrentPosition),
            watchPosition: String(navigator.geolocation.watchPosition)
        })
        """)
        print(hook_check)

        print("\n--- BUTTON ---")
        button = page.locator(
            'button.location-box.mask-button',
            has_text="Detect my location"
        ).first
        print("count:", await button.count())
        print("visible:", await button.is_visible() if await button.count() else False)

        print("\nClicking Detect and watching for 8 seconds...")
        before = len(console_lines)
        await button.click()
        await page.wait_for_timeout(8000)

        print("\n--- AFTER CLICK ---")
        print("Detect visible:", await button.is_visible())
        print("Location input:", await page.locator(
            'input[name="select-locality"]'
        ).count())

        print("\n--- EVENTS DURING CLICK ---")
        events = console_lines[before:]
        for line in events[-100:]:
            print(line)

        print("\n--- PAGE TEXT (LOCATION AREA) ---")
        text = await page.locator("body").inner_text()
        for line in text.splitlines():
            line = line.strip()
            if line and any(k in line.lower() for k in [
                "location", "delivery", "detect", "address", "unable", "permission"
            ]):
                print(line[:250])

        print("\n--- JS GEO DIRECT CALL AFTER CLICK ---")
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
                {enableHighAccuracy:true, timeout:5000}
            );
        })
        """)
        print(geo)

        print("\nBrowser left open. Press Ctrl+C to exit.")
        while True:
            await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
