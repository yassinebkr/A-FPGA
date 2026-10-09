"""Manual probe: visual/UI verification after frontend rebuild."""
import asyncio, json, urllib.request
from playwright.async_api import async_playwright

async def main():
    projs = json.load(urllib.request.urlopen("http://127.0.0.1:8001/api/projects"))
    target = next(p for p in projs if p["id"] == "proj-8b34f73401dc")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        closes = []
        page.on("console", lambda m: closes.append(m.text) if "websocket closed" in m.text else None)
        page.on("pageerror", lambda e: print("[pageerror]", e))
        await page.goto("http://localhost:3000/")
        await page.wait_for_timeout(2000)
        await page.click(f"text={target['name']}")
        await page.wait_for_timeout(4000)
        await page.click("text=PROJECTS")
        await page.wait_for_timeout(2000)
        await page.click(f"text={target['name']}")
        await page.wait_for_timeout(4000)

        pill = await page.locator("header").inner_text()
        print("header text:", " | ".join(pill.split()))
        print("unexpected closes:", len(closes))

        btns = await page.locator("button").all_inner_texts()
        print("buttons:", [b.strip() for b in btns if b.strip()][:15])

        # metric card values (leaf numeric nodes)
        nums = await page.evaluate(
            "() => Array.from(document.querySelectorAll('body *'))"
            ".filter(e => e.children.length === 0 && e.textContent && /^[0-9][0-9,.]*$/.test(e.textContent.trim()) && e.textContent.trim().length > 1)"
            ".map(e => e.textContent.trim()).slice(0, 15)"
        )
        print("numeric leaves:", nums)

        print("rendered <table> count:", await page.locator("table").count())

        # open Jobs tab if present
        for b in btns:
            if b.strip().startswith("Jobs"):
                await page.click(f"button:has-text('{b.strip()}')")
                await page.wait_for_timeout(1000)
                break
        await page.screenshot(path="/tmp/ui_check.png")
        print("screenshot saved")
        await browser.close()

asyncio.run(main())
