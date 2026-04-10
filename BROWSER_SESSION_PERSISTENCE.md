# Browser Session Persistence Implementation Guide

## Current State
- New browser instance created for **each** video download (expensive)
- No proxy support in browser (uses default connection)
- Session state not reused between calls

## Proposed Solution: Persistent Browser with Pooling

### Architecture Option 1: Single Persistent Browser (Recommended for your use case)

```python
class BrowserHandler:
    def __init__(self, config: dict):
        self.config = config
        self.browser = None
        self.context = None
        self.page = None
    
    async def initialize(self):
        """Initialize persistent browser once"""
        pw = await async_playwright().start()
        self.browser = await pw.chromium.launch(headless=True)
        self.context = await self.browser.new_context(...)
        self.page = await self.context.new_page()
    
    async def fetch_video_src_with_browser(self, embed_url, video_id):
        """Reuse existing page - just navigate to new URL"""
        await self.page.goto(embed_url, ...)
        # ... extract video
```

**Pros:**
- ✅ Minimal overhead - no restart between calls
- ✅ Automatic session/cookie reuse
- ✅ Memory efficient (one browser process)

**Cons:**
- ❌ No proxy support (Playwright browser context doesn't route through proxies by default)
- ⚠️ Page state accumulates (requires periodic cleanup)
- ⚠️ Single point of failure (crash affects all downloads)

### Architecture Option 2: Browser Pool with Proxy Support (Recommended if you need proxies)

```python
class BrowserPool:
    def __init__(self, config: dict, pool_size: int = 3):
        self.pool_size = pool_size
        self.browsers = []  # List of (browser, proxy_url) tuples
        self.available = asyncio.Queue()
    
    async def initialize(self):
        """Create browser instances, each tied to a proxy"""
        for i, proxy_url in enumerate(proxy_list[:self.pool_size]):
            browser = await pw.chromium.launch(
                proxy={"server": proxy_url}  # Important!
            )
            context = await browser.new_context()
            self.browsers.append(browser)
            await self.available.put((browser, proxy_url))
    
    async def fetch_with_proxy(self, embed_url, video_id, proxy_url):
        """Get browser from pool, use it, return it"""
        # Find or create browser for this proxy
        browser = await self._get_browser_for_proxy(proxy_url)
        page = await browser.new_context().new_page()
        
        await page.goto(embed_url, ...)
        # ... extract video
        
        await page.close()
        # Browser stays alive for reuse
```

**Pros:**
- ✅ **Supports proxies** (launch chromium with `proxy=` parameter)
- ✅ Multiple browsers = better fault isolation
- ✅ Each browser reuses its proxy connection

**Cons:**
- ⚠️ More memory (3-5 browser processes)
- ⚠️ Proxy switching requires new browser instance
- ❌ Still overhead vs HTTP fallback (but amortized over reuse)

## Implementation Recommendation

For your setup, I recommend **Option 2 (Browser Pool with Proxies)** because:

1. **Your current setup uses proxies** - the browser should too
2. **No overhead loss** - amortized over 10-100 requests per browser
3. **Resilience** - if one browser crashes, others continue
4. **Scalability** - easy to adjust pool_size based on load

## Proxy Support in Playwright

**Critical:** Playwright can use proxies at launch time:

```python
# Create browser with proxy
browser = await pw.chromium.launch(
    proxy={"server": "http://192.168.50.102:5080"}
)

# ALL requests through this browser use the proxy
await page.goto("https://www.tiktok.com/")  # Goes through proxy
```

**Limitations:**
- Proxy set at browser launch (cannot change mid-session)
- To use different proxy = new browser instance
- This is why a pool makes sense

## Integration Steps

1. **Modify BrowserHandler:**
   - Add `async def initialize()` - launch once on startup
   - Add `async def close()` - cleanup on shutdown
   - Keep single persistent `self.page`

2. **Modify TikTokScraper:**
   ```python
   async def __aenter__():
       self.browser_handler = BrowserHandler(config)
       await self.browser_handler.initialize()
       return self
   
   async def __aexit__():
       await self.browser_handler.close()
   ```

3. **Update scraper.py scrape() method:**
   ```python
   async with TikTokScraper(config, ...) as scraper:
       # BrowserHandler initialized once
       results = await scraper.scrape_batch(video_ids)
   ```

## Performance Estimate

| Approach | First Call | Per Call | Memory |
|----------|-----------|----------|--------|
| Current (new browser each) | ~8s | ~8s | ~300MB each |
| Persistent (no proxy) | ~8s | ~0.5s | ~300MB total |
| Pool of 3 (with proxies) | ~24s init | ~1-2s | ~900MB total |

For 100 videos:
- Current: 800s ❌
- Persistent: ~50s ✅
- Pool: ~100-200s + 24s init ✅

## Dealing with Proxy Rotation

If you need to **rotate proxies during scraping:**

```python
async def download_post_with_proxy(self, video_id, metadata, proxy_url):
    """Use browser pool to download through specific proxy"""
    if not self.browser_for_proxy.get(proxy_url):
        # Create new browser for this proxy
        self.browser_for_proxy[proxy_url] = await self._create_browser_with_proxy(proxy_url)
    
    browser = self.browser_for_proxy[proxy_url]
    # ... use browser
```

This creates browsers on-demand per proxy, so you get:
- ✅ Proxy support
- ✅ Reuse within same proxy
- ✅ No overhead for proxy rotation (just pick different browser)

## Summary

**Choose Option 1 (Single Persistent)** if:
- Proxies not needed for browser (only HTTP client uses them)
- Want simplest implementation
- Scraping isn't time-critical

**Choose Option 2 (Pool with Proxies)** if:
- Want to rotate browser proxies
- TikTok blocks based on IP (need proxy support)
- Want better fault tolerance
