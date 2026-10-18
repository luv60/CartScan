import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            geolocation={
                "latitude": 21.1458,
                "longitude": 79.0882,
                "accuracy": 10,
            },
            permissions=["geolocation"],
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )

        page = await context.new_page()

        page.on("console", lambda msg:
            print(f"[CONSOLE {msg.type}] {msg.text}")
        )

        page.on("pageerror", lambda exc:
            print(f"[PAGE ERROR] {exc}")
        )

        print("Opening Blinkit...")
        await page.goto(
            "https://blinkit.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )

        await page.wait_for_timeout(3000)

        print("\n================ BROWSER GEOLOCATION TEST ================\n")

        result = await page.evaluate("""
            () => new Promise((resolve) => {
                if (!navigator.geolocation) {
                    resolve({
                        supported: false,
                        error: "navigator.geolocation unavailable"
                    });
                    return;
                }

                navigator.geolocation.getCurrentPosition(
                    pos => resolve({
                        supported: true,
                        success: true,
                        latitude: pos.coords.latitude,
                        longitude: pos.coords.longitude,
                        accuracy: pos.coords.accuracy
                    }),
                    err => resolve({
                        supported: true,
                        success: false,
                        code: err.code,
                        message: err.message
                    }),
                    {
                        enableHighAccuracy: true,
                        timeout: 10000,
                        maximumAge: 0
                    }
                );
            })
        """)

        print(result)

        print("\n================ CLICKING DETECT ================\n")

        detect = page.get_by_text(
            "Detect my location",
            exact=True,
        )

        await detect.click()

        print("Click completed.")

        await page.wait_for_timeout(5000)

        print("\n================ AFTER CLICK ================\n")

        print(
            "Detect visible:",
            await detect.is_visible() if await detect.count() else "gone"
        )

        print(
            "Location input:",
            await page.locator('input[name="select-locality"]').count()
        )

        print("\nCurrent URL:")
        print(page.url)

        print("\n================ GEOLOCATION AGAIN ================\n")

        result2 = await page.evaluate("""
            () => new Promise((resolve) => {
                navigator.geolocation.getCurrentPosition(
                    pos => resolve({
                        success: true,
                        latitude: pos.coords.latitude,
                        longitude: pos.coords.longitude,
                        accuracy: pos.coords.accuracy
                    }),
                    err => resolve({
                        success: false,
                        code: err.code,
                        message: err.message
                    })
                );
            })
        """)

        print(result2)

        print("\nBrowser remains open. Press ENTER to close.")
        await asyncio.to_thread(input)

        await browser.close()

asyncio.run(main())
