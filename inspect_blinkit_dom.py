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
            },
            permissions=["geolocation"],
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )

        page = await context.new_page()

        print("Opening Blinkit...")
        await page.goto(
            "https://blinkit.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )

        await page.wait_for_timeout(3000)

        print("\n================ BUTTONS ================\n")

        buttons = await page.locator("button").all()
        for idx, b in enumerate(buttons):
            try:
                print(
                    f"[BUTTON {idx}] "
                    f"text={repr(await b.inner_text())} "
                    f"aria={repr(await b.get_attribute('aria-label'))} "
                    f"title={repr(await b.get_attribute('title'))}"
                )
            except:
                pass

        print("\n================ INPUTS ================\n")

        inputs = await page.locator("input").all()
        for idx, i in enumerate(inputs):
            try:
                print(
                    f"[INPUT {idx}] "
                    f"placeholder={repr(await i.get_attribute('placeholder'))} "
                    f"type={repr(await i.get_attribute('type'))} "
                    f"aria={repr(await i.get_attribute('aria-label'))} "
                    f"name={repr(await i.get_attribute('name'))}"
                )
            except:
                pass

        print("\n================ TEXT ================\n")

        body_text = await page.locator("body").inner_text()
        print(body_text[:10000])

        print("\n================ DETECT LOCATION ================\n")

        detect = page.get_by_text("Detect my location", exact=True)

        print("count:", await detect.count())

        if await detect.count():
            print("visible:", await detect.first.is_visible())
            print("tag:", await detect.first.evaluate("(e) => e.tagName"))
            print("outerHTML:")
            print(
                (await detect.first.evaluate("(e) => e.outerHTML"))[:5000]
            )

        print("\nBrowser will remain open.")
        print("Inspect the page visually, then press ENTER here to close.")

        await asyncio.to_thread(input)

        await browser.close()

asyncio.run(main())
