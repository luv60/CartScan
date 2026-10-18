import asyncio
from playwright.async_api import async_playwright

async def main():
    print("Starting Playwright...")
    async with async_playwright() as p:
        print("Playwright started")
        browser = await p.chromium.launch(headless=False)
        print("Chromium started")
        page = await browser.new_page()
        await page.goto("https://www.google.com")
        print("Browser page opened")
        await asyncio.sleep(5)
        await browser.close()

asyncio.run(main())
