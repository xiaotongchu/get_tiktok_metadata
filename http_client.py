"""
HTTP client using httpx for true async requests (replaces requests + FuturesSession).
Provides a clean async interface for fetching metadata and media with per-request proxy support.
"""
import httpx
from typing import Optional, Dict, Any
import json
from bs4 import BeautifulSoup


class TikTokHTTPClient:
    """
    Async HTTP client for TikTok requests using httpx.
    
    Key features:
    - True async I/O (not thread pool)
    - Per-request proxy support via client caching
    - Connection pooling maintained per proxy
    - Better error handling
    - Cleaner API than requests
    """
    
    # Standard headers for embed page requests
    EMBED_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "DNT": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:101.0) Gecko/20100101 Firefox/101.0"
    }
    
    # Headers for video downloads
    VIDEO_DOWNLOAD_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/110.0",
        "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Referer": "https://www.tiktok.com/",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
        "Accept-Encoding": "identity"
    }
    
    # Headers for image downloads
    IMAGE_DOWNLOAD_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/110.0",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Referer": "https://www.tiktok.com/",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
        "Accept-Encoding": "identity"
    }
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize HTTP client with client caching for per-request proxy support.
        
        Args:
            config: Configuration dict with timeout, connect_timeout, etc.
        """
        self.config = config
        self.timeout = httpx.Timeout(
            timeout=config.get('timeout', 30),
            connect=config.get('connect_timeout', 10)
        )
        
        # Cache of httpx.AsyncClient instances, one per proxy
        # Key: proxy URL (or "__default__" for no proxy)
        self.client_cache: Dict[str, httpx.AsyncClient] = {}
    
    async def _get_client(self, proxy: Optional[str] = None) -> httpx.AsyncClient:
        """
        Get or create an httpx.AsyncClient for the given proxy.
        
        Each proxy gets its own client instance with dedicated connection pooling.
        This allows true per-request proxy rotation while maintaining efficiency.
        
        Args:
            proxy: Proxy URL (or None for direct connection)
            
        Returns:
            httpx.AsyncClient configured for this proxy
        """
        # Use "__default__" as key for no-proxy requests
        cache_key = proxy or "__default__"
        
        # Return cached client if available
        if cache_key in self.client_cache:
            return self.client_cache[cache_key]
        
        # Create new client for this proxy
        limits = httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5
        )
        
        client = httpx.AsyncClient(
            proxy=proxy,  # Set proxy at client initialization
            limits=limits,
            timeout=self.timeout,
            follow_redirects=True
        )
        
        self.client_cache[cache_key] = client
        return client
    
    async def fetch_embed_page(self, video_id: str, proxy: Optional[str] = None) -> Optional[str]:
        """
        Fetch the embed page for a video.
        
        Args:
            video_id: TikTok video ID
            proxy: Proxy URL to use (or None for direct)
            
        Returns:
            HTML content of embed page, or None if failed
            
        Raises:
            httpx.RequestError: On network errors
        """
        url = f"https://www.tiktok.com/embed/v2/{video_id}"
        
        try:
            client = await self._get_client(proxy)
            response = await client.get(url, headers=self.EMBED_HEADERS)
            response.raise_for_status()  # Raise on 4xx/5xx
            return response.text
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                print(f"⚠️  Video {video_id} not found (404)")
            else:
                print(f"⚠️  HTTP {e.response.status_code} for embed page")
            raise
    
    async def download_media(
        self,
        url: str,
        media_type: str = "video",
        proxy: Optional[str] = None,
        chunk_size: int = 1024 * 1024
    ) -> Optional[bytes]:
        """
        Download media (video or image) from a URL in chunks.
        
        Args:
            url: Media URL to download
            media_type: "video" or "image"
            proxy: Proxy URL to use
            chunk_size: Bytes per chunk
            
        Returns:
            Media bytes, or None if failed (HTTP 403)
            
        Raises:
            httpx.RequestError: On network errors
        """
        headers = self.VIDEO_DOWNLOAD_HEADERS if media_type == "video" else self.IMAGE_DOWNLOAD_HEADERS
        
        try:
            client = await self._get_client(proxy)
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                
                content = b""
                async for chunk in response.aiter_bytes(chunk_size):
                    if chunk:
                        content += chunk
                
                return content
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                return None  # Signal that browser fallback should be used
            raise
    
    async def get_content_type(self, url: str, proxy: Optional[str] = None) -> Optional[str]:
        """
        Get Content-Type header from a URL without downloading full file.
        Useful for determining file extensions.
        
        Args:
            url: URL to check
            proxy: Proxy URL to use
            
        Returns:
            Content-Type header value, or None if failed
        """
        try:
            client = await self._get_client(proxy)
            response = await client.head(url)
            response.raise_for_status()
            return response.headers.get("content-type", "")
        except Exception:
            # If HEAD fails, try GET with Range header
            try:
                client = await self._get_client(proxy)
                response = await client.get(url, headers={"Range": "bytes=0-0"})
                return response.headers.get("content-type", "")
            except Exception:
                return None
    
    async def close(self):
        """Close all cached HTTP client sessions."""
        for proxy_url, client in self.client_cache.items():
            try:
                await client.aclose()
            except Exception as e:
                print(f"⚠️  Error closing client for {proxy_url}: {e}")
        
        self.client_cache.clear()
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

