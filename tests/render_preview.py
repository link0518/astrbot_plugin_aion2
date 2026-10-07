"""把卡片 HTML 截成图片，用于离线检查外观。

AstrBot 运行时用 Playwright 渲染，这里用同一套参数截图，尽量贴近真实输出。
"""

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path(__file__).resolve().parent / "out"


async def main() -> int:
    targets = sorted(OUT.glob("card_*.html"))
    if not targets:
        print("没有找到卡片 HTML，请先运行 tests/test_live.py")
        return 1

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 800, "height": 600}, device_scale_factor=2)
        for html in targets:
            await page.goto(html.resolve().as_uri())
            await page.wait_for_timeout(400)
            card = page.locator(".card")
            shot = OUT / f"{html.stem}.png"
            await card.screenshot(path=str(shot))
            size = await card.bounding_box()
            print(f"  {shot.name}  {int(size['width'])}x{int(size['height'])}")
        await browser.close()
    return 0


sys.exit(asyncio.run(main()))
