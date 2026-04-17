"""
Browser pool management for headless browsing with proxy support.
Maintains persistent browser instances (one per proxy) to avoid repeated browser spinup.
Limits resource usage by capping the number of concurrent browsers.
"""
import asyncio
from typing import Dict, List, Optional, Any
from playwright.async_api import async_playwright, Browser


class BrowserPool:
    """
    Manages a pool of persistent browser instances, one per proxy.
    
    Features:
    - Limits concurrent browsers to save RAM (configurable, default 3)
    - Uses proxies for each browser (or no proxy for localhost)
    - Removes browsers when their proxy is marked broken
    - Can add new browsers when proxies become available
    """
    
    def __init__(self, proxy_list: List[str], config: dict, max_browsers: int = 3):
        """
        Initialize browser pool (not yet started).
        
        Args:
            proxy_list: List of all available proxies from config
            config: Browser configuration from config.yaml
            max_browsers: Maximum number of concurrent browsers (default 3)
        """
        self.proxy_list = proxy_list
        self.config = config
        self.max_browsers = max_browsers
        
        self.browsers: Dict[str, Browser] = {}  # proxy_url -> browser instance
        self.browsers_in_use: Dict[str, int] = {}  # proxy_url -> usage count
        self._playwright = None
        self._pw = None
        self._initialized = False
    
    async def initialize(self) -> None:
        """
        Initialize browser instances for the first N proxies.
        Should be called at start of scrape_batch.
        """
        if self._initialized:
            return
        
        # Take up to max_browsers proxies from the list
        proxies_to_use = self.proxy_list[:self.max_browsers]
        
        # Initialize Playwright
        self._pw = async_playwright()
        self._playwright = await self._pw.__aenter__()
        
        # Create browsers for selected proxies
        for proxy in proxies_to_use:
            try:
                browser = await self._create_browser(proxy)
                self.browsers[proxy] = browser
                self.browsers_in_use[proxy] = 0
                proxy_display = proxy if proxy != "__localhost__" else "localhost"
                print(f"🌐 Browser pool: Initialized browser for {proxy_display}")
            except Exception as e:
                print(f"⚠️  Browser pool: Failed to initialize browser for {proxy}: {e}")
        
        self._initialized = True
    
    async def _create_browser(self, proxy: str) -> Browser:
        """
        Create a new browser instance for a proxy.
        
        Args:
            proxy: Proxy URL or "__localhost__"
            
        Returns:
            Playwright Browser instance
        """
        proxy_arg = None
        if proxy != "__localhost__":
            proxy_arg = {"server": proxy}
        
        browser = await self._playwright.chromium.launch(
            headless=self.config.get('headless', True),
            proxy=proxy_arg
        )
        return browser
    
    async def get_browser_for_proxy(self, proxy: str) -> Optional[Browser]:
        """
        Get browser for a proxy (will use existing if available).
        Creates a new one if under max_browsers limit and not already in use.
        
        Args:
            proxy: Proxy URL to use
            
        Returns:
            Browser instance, or None if cannot get/create one
        """
        # If we already have a browser for this proxy, increment usage counter
        if proxy in self.browsers:
            self.browsers_in_use[proxy] = self.browsers_in_use.get(proxy, 0) + 1
            return self.browsers[proxy]
        
        # If we can create a new one (under max)
        if len(self.browsers) < self.max_browsers:
            try:
                browser = await self._create_browser(proxy)
                self.browsers[proxy] = browser
                self.browsers_in_use[proxy] = 1
                proxy_display = proxy if proxy != "__localhost__" else "localhost"
                print(f"🌐 Browser pool: Created new browser for {proxy_display} (count: {len(self.browsers)}/{self.max_browsers})")
                return browser
            except Exception as e:
                print(f"⚠️  Browser pool: Failed to create browser for {proxy}: {e}")
                return None
        
        # Cannot get/create browser
        return None
    
    def release_browser(self, proxy: str) -> None:
        """
        Decrement usage count for a browser (called after use).
        
        Args:
            proxy: Proxy URL that was used
        """
        if proxy in self.browsers_in_use:
            self.browsers_in_use[proxy] = max(0, self.browsers_in_use[proxy] - 1)
    
    async def remove_browser_for_proxy(self, proxy: str) -> None:
        """
        Remove and close browser for a broken proxy.
        Called when proxy is marked as broken.
        
        Args:
            proxy: Proxy URL to remove
        """
        if proxy in self.browsers:
            try:
                await self.browsers[proxy].close()
            except Exception as e:
                print(f"⚠️  Browser pool: Error closing browser for {proxy}: {e}")
            
            del self.browsers[proxy]
            self.browsers_in_use.pop(proxy, None)
            proxy_display = proxy if proxy != "__localhost__" else "localhost"
            print(f"🌐 Browser pool: Removed browser for {proxy_display}")
        
        # Try to add a new browser from available proxies
        await self._try_add_replacement_browser()
    
    async def _try_add_replacement_browser(self) -> None:
        """
        Try to add a new browser for an unused proxy to maintain pool capacity.
        Called after a browser is removed.
        """
        # Find proxies not yet in the pool
        unused_proxies = [p for p in self.proxy_list if p not in self.browsers]
        
        for proxy in unused_proxies:
            if len(self.browsers) >= self.max_browsers:
                break  # Pool is full
            
            try:
                browser = await self._create_browser(proxy)
                self.browsers[proxy] = browser
                self.browsers_in_use[proxy] = 0
                proxy_display = proxy if proxy != "__localhost__" else "localhost"
                print(f"🌐 Browser pool: Added replacement browser for {proxy_display} (count: {len(self.browsers)}/{self.max_browsers})")
                break  # Only add one per call
            except Exception as e:
                print(f"⚠️  Browser pool: Failed to create replacement browser for {proxy}: {e}")
    
    async def close_all(self) -> None:
        """Close all browser instances and cleanup."""
        for proxy, browser in list(self.browsers.items()):
            try:
                await browser.close()
            except Exception as e:
                print(f"⚠️  Browser pool: Error closing browser for {proxy}: {e}")
        
        self.browsers.clear()
        self.browsers_in_use.clear()
        
        if self._pw:
            try:
                await self._pw.__aexit__(None, None, None)
            except Exception as e:
                print(f"⚠️  Browser pool: Error closing Playwright: {e}")
        
        self._initialized = False
        print("🌐 Browser pool: Closed all browsers")
    
    def get_pool_status(self) -> str:
        """Get current pool status for logging."""
        active = sum(1 for count in self.browsers_in_use.values() if count > 0)
        return f"{len(self.browsers)}/{self.max_browsers} browsers active, {active} in use"
