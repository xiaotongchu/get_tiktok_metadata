"""
Browser-based fallback handler for edge cases (e.g., HTTP 403 responses).
Using Playwright for headless Chrome automation.
"""
import asyncio
import random
import time
import json
from typing import Tuple, Optional
from playwright.async_api import async_playwright, Browser


class BrowserHandler:
    """
    Handles browser-based fallback for downloading videos when standard
    HTTP requests fail (e.g., 403 responses).
    
    Improvements:
    - Isolated from main flow
    - Better error handling with logging
    - Reusable browser instance support
    - Proxy-aware when using pooled browsers
    """
    
    def __init__(self, config: dict):
        """
        Initialize browser handler.
        
        Args:
            config: Browser configuration from config.yaml
        """
        self.config = config
        self.browser = None
        self.proxy = None
    
    async def fetch_video_src_with_browser(
        self,
        embed_url: str,
        video_id: str,
        browser: Optional[Browser] = None,
        proxy: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[bytes]]:
        """
        Use a headless browser to load the embed page and extract video URL.
        
        Args:
            embed_url: URL to the TikTok embed page (https://www.tiktok.com/embed/v2/{id})
            video_id: Video ID (for logging)
            browser: Optional existing browser instance from pool (if None, creates new one)
            proxy: Proxy URL being used (for logging)
            
        Returns:
            Tuple of (video_src_url, video_data_bytes)
            - video_src_url: The extracted video source URL (may be None)
            - video_data_bytes: Downloaded video bytes (may be None)
        """
        src_holder = {"url": None}
        video_data = None
        self.proxy = proxy
        pw = None
        context = None
        page = None
        
        try:
            # If browser provided from pool, use it directly; otherwise create new
            if browser:
                # Use provided browser from pool
                proxy_display = proxy if proxy and proxy != "__localhost__" else "localhost"
                print(f"🌐 Browser (pooled): Using browser for {proxy_display}")
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Mobile Safari/537.36",
                    viewport={"width": 390, "height": 844},
                    device_scale_factor=2,
                    locale="en-NL",
                )
                page = await context.new_page()
                should_close_browser = False
            else:
                # Create new browser (legacy mode)
                pw = async_playwright()
                pw_obj = await pw.__aenter__()
                browser = await pw_obj.chromium.launch(headless=self.config.get('headless', True))
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Mobile Safari/537.36",
                    viewport={"width": 390, "height": 844},
                    device_scale_factor=2,
                    locale="en-NL",
                )
                page = await context.new_page()
                should_close_browser = True
            
            # Common flow for both pooled and legacy browsers
            try:
                # First, establish session by visiting main site
                print(f"🌐 Browser: Establishing session at tiktok.com...")
                await page.goto(
                    "https://www.tiktok.com/",
                    wait_until="domcontentloaded",
                    timeout=self.config.get('navigate_timeout', 30) * 1000
                )
                await page.wait_for_timeout(3000 + random.randint(0, 2000))  # Wait a bit for any session cookies to set
            except Exception as e:
                print(f"⚠️  Browser: Could not access tiktok.com: {e}")
            
            try:
                # Navigate to embed page
                print(f"🌐 Browser: Loading embed page for {video_id}...")
                await page.goto(
                    embed_url,
                    wait_until="domcontentloaded",
                    timeout=self.config.get('navigate_timeout', 30) * 1000
                )
            except Exception as e:
                print(f"⚠️  Browser: Could not load embed page: {e}")
            
            try:
                # Wait for video element to appear
                print(f"🌐 Browser: Waiting for video element...")
                await page.wait_for_selector(
                    "video",
                    timeout=self.config.get('wait_for_video_timeout', 12) * 1000
                )
            except Exception:
                print(f"⚠️  Browser: Video element not found within timeout")
            
            # JavaScript to extract video src
            video_selectors_json = json.dumps([
                '[data-e2e="embed-swiper-SwiperVideoPlayer-DivSwiperVideoItem"] video[src]',
                '[data-e2e="Player-index-EmbedPlayerContainer"] video[src]',
                'video[src]'
            ])
            
            get_video_src_code = f"""
            () => {{
                const selectors = {video_selectors_json};
                for (const selector of selectors) {{
                    const v = document.querySelector(selector);
                    if (v && v.src) return v.src;
                }}
                return null;
            }}
            """
            
            # Extract initial video src
            print(f"🌐 Browser: Extracting video source...")
            try:
                video_src = await page.evaluate(get_video_src_code)
                if video_src:
                    src_holder["url"] = video_src
                    print(f"🌐 Browser: Found video src")
                
                # Wait for src to update (indicates JS has processed the page)
                now = time.time()
                timeout_secs = self.config.get('timeout', 50)
                
                while time.time() - now < timeout_secs:
                    new_video_src = await page.evaluate(get_video_src_code)
                    if new_video_src and new_video_src != video_src:
                        print(f"🌐 Browser: Video src updated")
                        video_src = new_video_src
                        src_holder["url"] = video_src
                        break
                    await page.wait_for_timeout(50)
                
            except Exception as e:
                print(f"⚠️  Browser: Error evaluating JavaScript: {e}")
            
            # Download video using browser's request context
            if src_holder.get("url"):
                try:
                    print(f"🌐 Browser: Downloading video from extracted URL...")
                    extra_headers = {
                        "accept": "*/*",
                        "accept-language": "nl,en-US;q=0.9,en;q=0.8",
                        "accept-encoding": "identity;q=1, *;q=0",
                        "priority": "i",
                        "range": "bytes=0-",
                        "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
                        "sec-ch-ua-mobile": "?1",
                        "sec-ch-ua-platform": '"Android"',
                        "sec-fetch-dest": "video",
                        "sec-fetch-mode": "no-cors",
                        "sec-fetch-site": "same-site",
                        "referer": "https://www.tiktok.com/",
                    }
                    
                    resp = await page.request.get(src_holder["url"], headers=extra_headers)
                    
                    # Accept both 200 (OK) and 206 (Partial Content)
                    if resp.status in [200, 206]:
                        video_data = await resp.body()
                        print(f"✓ Browser: Downloaded {len(video_data)} bytes")
                    else:
                        print(f"⚠️  Browser: HTTP {resp.status} downloading video")
                
                except Exception as e:
                    print(f"⚠️  Browser: Error downloading video: {e}")
            
            # Cleanup context (always, for both pooled and legacy)
            if context:
                try:
                    await context.close()
                except Exception as e:
                    print(f"⚠️  Browser: Error closing context: {e}")
            
            # Close browser only in legacy mode
            if should_close_browser and browser:
                try:
                    await browser.close()
                except Exception as e:
                    print(f"⚠️  Browser: Error closing browser: {e}")
        
        except Exception as e:
            print(f"❌ Browser fallback failed: {e}")
        
        finally:
            # Cleanup playwright in legacy mode
            if pw:
                try:
                    await pw.__aexit__(None, None, None)
                except Exception as e:
                    print(f"⚠️  Browser: Error closing Playwright: {e}")
        
        return src_holder.get("url"), video_data
    
    async def close(self):
        """Clean up browser resources."""
        if self.browser:
            await self.browser.close()

