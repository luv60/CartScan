import asyncio
import json
from playwright.async_api import async_playwright

LAT = 21.1458
LON = 79.0882

GEO_SCRIPT = f"""
(() => {{
    const latitude = {LAT!r};
    const longitude = {LON!r};
    const position = {{
        coords: {{
            latitude, longitude, accuracy: 10,
            altitude: null, altitudeAccuracy: null,
            heading: null, speed: null
        }},
        timestamp: Date.now()
    }};
    const geolocation = {{
        getCurrentPosition(success, error, options) {{
            console.log("[GEO-HOOK] getCurrentPosition", JSON.stringify(options || {{}}));
            if (typeof success === "function") success(position);
        }},
        watchPosition(success, error, options) {{
            console.log("[GEO-HOOK] watchPosition", JSON.stringify(options || {{}}));
            if (typeof success === "function") success(position);
            return 1;
        }},
        clearWatch(id) {{}}
    }};
    try {{
        Object.defineProperty(Navigator.prototype, "geolocation", {{
            configurable: true, get: () => geolocation
        }});
    }} catch (e) {{
        try {{
            Object.defineProperty(navigator, "geolocation", {{
                configurable: true, get: () => geolocation
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

        interesting = []
        page.on("request", lambda req: (
            interesting.append({
                "kind": "request",
                "method": req.method,
                "url": req.url,
                "post_data": req.post_data,
                "headers": {k:v for k,v in req.headers.items()
                            if k.lower() in ["content-type", "origin", "referer"]}
            })
            if any(x in req.url.lower() for x in [
                "location", "address", "gps", "geo", "locality",
                "store", "service", "delivery", "place"
            ]) else None
        ))

        async def on_response(resp):
            if not any(x in resp.url.lower() for x in [
                "location", "address", "gps", "geo", "locality",
                "store", "service", "delivery", "place"
            ]):
                return
            item = {
                "kind": "response",
                "status": resp.status,
                "url": resp.url,
                "content_type": resp.headers.get("content-type", "")
            }
            try:
                body = await resp.text()
                item["body"] = body[:12000]
            except Exception as e:
                item["body_error"] = str(e)
            interesting.append(item)

        page.on("response", on_response)

        console = []
        page.on("console", lambda msg: console.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda exc: console.append(f"[PAGEERROR] {exc}"))

        print("Opening Blinkit...")
        await page.goto("https://blinkit.com/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        button = page.locator(
            'button.location-box.mask-button',
            has_text="Detect my location"
        ).first

        print("Detect button visible:", await button.is_visible())
        print("Clearing captured requests and clicking Detect...")
        interesting.clear()
        console.clear()

        await button.click()
        await page.wait_for_timeout(10000)

        print("\n================ REQUESTS ================")
        for x in interesting:
            if x["kind"] == "request":
                print("\nREQUEST")
                print("METHOD:", x["method"])
                print("URL:", x["url"])
                print("POST:", x["post_data"])
                print("HEADERS:", x["headers"])

        print("\n================ RESPONSES ================")
        for x in interesting:
            if x["kind"] == "response":
                print("\nRESPONSE")
                print("STATUS:", x["status"])
                print("URL:", x["url"])
                print("CONTENT-TYPE:", x["content_type"])
                print("BODY:")
                print(x.get("body", "")[:12000])

        print("\n================ CONSOLE ================")
        for x in console[-100:]:
            print(x)

        print("\n================ PAGE STATE ================")
        print("Detect visible:", await button.is_visible())
        print("Location input:", await page.locator('input[name="select-locality"]').count())
        body = await page.locator("body").inner_text()
        for line in body.splitlines():
            line = line.strip()
            if line and any(k in line.lower() for k in [
                "location", "delivery", "detect", "available", "different"
            ]):
                print(line[:300])

        print("\nBrowser left open. Press Ctrl+C to exit.")
        while True:
            await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
