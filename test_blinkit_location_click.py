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

        detect = page.get_by_text(
            "Detect my location",
            exact=True,
        )

        print("\nDetect button count:", await detect.count())
        print("Detect visible:", await detect.first.is_visible())

        print("\nCLICKING DETECT MY LOCATION...")
        await detect.first.click()

        print("Click completed.")

        await page.wait_for_timeout(3000)

        print("\n================ AFTER CLICK ================\n")

        print("Detect button count:", await detect.count())

        if await detect.count():
            try:
                print("Detect visible:", await detect.first.is_visible())
            except:
                print("Detect visible: element detached")

        print("\nINPUTS:")

        for idx, i in enumerate(await page.locator("input").all()):
            try:
                print(
                    f"[INPUT {idx}] "
                    f"placeholder={repr(await i.get_attribute('placeholder'))} "
                    f"type={repr(await i.get_attribute('type'))} "
                    f"name={repr(await i.get_attribute('name'))}"
                )
            except:
                pass

        print("\nSEARCH INPUT CANDIDATES:")

        selector = (
            'input[placeholder*="Search"], '
            'input[placeholder*="search"], '
            'input[type="search"]'
        )

        candidates = await page.locator(selector).all()

        print("count:", len(candidates))

        for idx, i in enumerate(candidates):
            try:
                print(
                    f"[SEARCH CANDIDATE {idx}] "
                    f"placeholder={repr(await i.get_attribute('placeholder'))} "
                    f"name={repr(await i.get_attribute('name'))} "
                    f"visible={await i.is_visible()}"
                )
            except:
                pass

        print("\nPAGE TEXT AFTER CLICK:")
        print((await page.locator("body").inner_text())[:5000])

        print("\nBrowser remains open. Press ENTER to close.")
        await asyncio.to_thread(input)

        await browser.close()

asyncio.run(main())
