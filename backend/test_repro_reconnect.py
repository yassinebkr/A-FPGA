"""Manual probe: reproduce the in-project 'reconnecting' bug in a real browser."""
import asyncio, json, urllib.request
from playwright.async_api import async_playwright

async def main():
    projs = json.load(urllib.request.urlopen("http://127.0.0.1:8001/api/projects"))
    target = next((p for p in projs if p["id"] == "proj-8b34f73401dc"), projs[0])
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        page.on("console", lambda m: print(f"[console:{m.type}] {m.text[:200]}"))
        page.on("pageerror", lambda e: print(f"[pageerror] {e}"))
        await page.goto("http://localhost:3000/")
        await page.wait_for_timeout(3000)
        # click into the project
        await page.click(f"text={target['name']}")
        await page.wait_for_timeout(2000)
        for i in range(6):
            status = await page.evaluate(
                "() => document.body.innerText.match(/connected|reconnecting/gi)?.slice(-3)"
            )
            print(f"t+{(i+1)*3}s status snippet: {status}")
            await page.wait_for_timeout(3000)
        await browser.close()

asyncio.run(main())
