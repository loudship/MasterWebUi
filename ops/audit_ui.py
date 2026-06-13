import asyncio
import os
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        print("Connecting to remote Browserless Chrome...")
        browser = await p.chromium.connect_over_cdp("ws://browserless:3000")
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        
        # Step 1: Navigating to Auth
        print("Navigating to Open WebUI login page...")
        await page.goto("http://open-webui:8080/auth")
        
        # Wait for the email input
        print("Waiting for login input fields...")
        await page.wait_for_selector("input[type='email']", timeout=15000)
        
        # Fill in details
        await page.fill("input[type='email']", "christianoallen618@gmail.com")
        await page.fill("input[type='password']", "Password123!")
        
        # Submit the form
        print("Submitting login form...")
        await page.click("button[type='submit']")
        
        # Wait for redirection to dashboard
        print("Waiting for dashboard redirect...")
        try:
            await page.wait_for_url("http://open-webui:8080/", timeout=15000)
        except Exception as e:
            print(f"Warning: URL did not redirect to root homepage: {e}. Current URL: {page.url}")
        
        await page.wait_for_timeout(3000)
        await page.screenshot(path="/app/pillar_dashboard.png")
        print("Saved pillar_dashboard.png")
        
        # Step 2: Navigate and capture menus
        menus = {
            "models": "http://open-webui:8080/workspace/models",
            "knowledge": "http://open-webui:8080/workspace/knowledge",
            "prompts": "http://open-webui:8080/workspace/prompts",
            "skills": "http://open-webui:8080/workspace/skills",
            "functions": "http://open-webui:8080/workspace/functions"
        }
        
        for name, url in menus.items():
            print(f"Navigating to {name} page: {url} ...")
            try:
                await page.goto(url, wait_until="networkidle")
            except Exception:
                await page.goto(url)
            await page.wait_for_timeout(3000)
            screenshot_path = f"/app/pillar_{name}.png"
            await page.screenshot(path=screenshot_path)
            print(f"Captured and saved {screenshot_path}")
            
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
