import sys
import re
from playwright.sync_api import sync_playwright
import urllib.parse
from skyscanner import GoogleFlightsScraper

def test():
    q = "REC to PVH 2026-06-16 one way"
    url = f"https://www.google.com/travel/flights?q={urllib.parse.quote(q)}&hl=pt-BR&gl=BR&curr=BRL"
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(locale="pt-BR", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        page = ctx.new_page()
        page.set_default_timeout(60000)
        
        page.goto(url, wait_until="domcontentloaded")
        
        # Wait until progress bar disappears!
        # The progress bar is usually a linear progress or something
        print("Waiting for page load...")
        import time
        
        # Wait 20 full seconds
        time.sleep(20)
        
        txt = page.locator("body").inner_text()
        m = re.search(r"Menores preços.*?R\$\s*([\d\.]+(?:,\d{2})?)", txt, flags=re.IGNORECASE | re.DOTALL)
        print("Price after 20 seconds:", m.group(1) if m else "No match")
        b.close()

if __name__ == "__main__":
    test()
