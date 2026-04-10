"""
Main orchestrator for TikTok scraper.
Coordinates metadata fetching, download URL extraction, and media downloads.

Key differences from original:
- True async patterns (no polling)
- Pipelined execution (metadata + downloads run concurrently, not in phases)
- Intelligent proxy pool with exponential backoff and circuit breaker
- Parallel image downloads
- Structured error handling with retry strategies
"""
import asyncio
import time
import yaml
import httpx
import json
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from collections import deque

from models import DownloadResult, DownloadStatus, VideoMetadata
from proxy_pool import ProxyPool
from http_client import TikTokHTTPClient
from metadata_extractor import MetadataExtractor
from media_downloader import MediaDownloader
from browser_handler import BrowserHandler


class TikTokScraper:
    """
    Main orchestrator for scraping TikTok posts.
    
    Architecture:
    - Async-first with httpx (not thread-pool requests)
    - Proxy pool with intelligent backoff
    - Pipelined execution (parallel metadata + download phases)
    - Structured retry handling per error type
    """
    
    def __init__(self, config_path: str = "config.yaml", output_dir: str = None):
        """
        Initialize scraper.
        
        Args:
            config_path: Path to configuration YAML file
            output_dir: Optional output directory (absolute or relative). 
                       Videos and images directories from config will be resolved relative to this.
                       If not provided, they're used as-is from config.
        """
        # Load configuration
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)
        
        # Initialize components
        self.http_client = TikTokHTTPClient(self.config['http_client'])
        self.proxy_pool = ProxyPool(
            self.config['proxy_pool']['proxies'],
            self.config['proxy_pool']
        )
        self.browser_handler = BrowserHandler(self.config['browser'])
        
        # Paths: resolve relative to output_dir if provided
        self.output = self.config['output'].copy()
        
        if output_dir:
            output_base = Path(output_dir).resolve()
            self.output['videos_dir'] = str(output_base / self.output['videos_dir'])
            self.output['images_dir'] = str(output_base / self.output['images_dir'])
        
        Path(self.output['videos_dir']).mkdir(parents=True, exist_ok=True)
        Path(self.output['images_dir']).mkdir(parents=True, exist_ok=True)
        
        # Tracking
        self.results: Dict[str, DownloadResult] = {}
        self.consecutive_failures = 0
    
    def _calculate_retry_delay(self, error_type: str, retry_count: int) -> float:
        """
        Calculate backoff delay based on error type and retry strategy from config.
        
        Args:
            error_type: Type of error (connection, timeout, http_403, http_error)
            retry_count: Current retry attempt (1-indexed)
            
        Returns:
            Delay in seconds before next retry
        """
        retry_config = self.config['retry'].get(error_type, {})
        backoff_type = retry_config.get('backoff_type', 'exponential')
        
        # Base delay
        base_delay = 1.0
        backoff_factor = self.config['http_client'].get('backoff_factor', 2.0)
        
        if backoff_type == 'exponential':
            # 1s, 2s, 4s, 8s, etc.
            delay = base_delay * (backoff_factor ** (retry_count - 1))
        elif backoff_type == 'linear':
            # 1s, 2s, 3s, 4s, etc.
            delay = base_delay * retry_count
        else:  # 'fixed'
            # Always same delay
            delay = base_delay
        
        # Don't exceed max configured timeout
        max_timeout = self.config['http_client'].get('timeout', 30)
        return min(delay, max_timeout)
    
    def _get_error_type(self, error: Exception) -> str:
        """
        Classify an exception to determine retry strategy.
        
        Args:
            error: The exception that occurred
            
        Returns:
            Error type key (connection_errors, timeout_errors, http_403_errors, http_errors)
        """
        if isinstance(error, asyncio.TimeoutError):
            return 'timeout_errors'
        elif isinstance(error, (ConnectionError, httpx.ConnectError, httpx.ProxyError)):
            return 'connection_errors'
        elif isinstance(error, httpx.HTTPStatusError):
            if error.response.status_code == 403:
                return 'http_403_errors'
            else:
                return 'http_errors'
        elif isinstance(error, httpx.RequestError):
            return 'connection_errors'
        else:
            return 'connection_errors'  # Default to connection errors
    
    async def _apply_retry_delay(self, error_type: str, retry_count: int, video_id: str) -> Tuple[float, int]:
        """
        Apply appropriate delay before retry based on error type.
        
        Args:
            error_type: Type of error
            retry_count: Current retry attempt
            video_id: Video ID (for logging)
            
        Returns:
            Tuple of (delay_applied, new_timeout) for timeout_errors
        """
        retry_config = self.config['retry'].get(error_type, {})
        max_attempts = retry_config.get('max_attempts', 3)
        
        if error_type == 'timeout_errors':
            # For timeout errors, increase timeout on next retry
            increment_timeout = retry_config.get('increment_timeout', 10)
            new_timeout = self.config['http_client']['timeout'] + (increment_timeout * retry_count)
            delay = self._calculate_retry_delay(error_type, retry_count)
            return delay, new_timeout
        else:
            delay = self._calculate_retry_delay(error_type, retry_count)
            return delay, None
    
    async def fetch_metadata_for_post(self, video_id: str) -> Tuple[Optional[VideoMetadata], Optional[str]]:
        """
        Fetch metadata for a single post.
        
        Args:
            video_id: TikTok post ID
            
        Returns:
            Tuple of (VideoMetadata, raw_json_string) if successful, (None, None) if failed
        """
        retry_count = 0
        max_retries = self.config['http_client']['max_retries']
        start_time = time.time()
        proxy_wait_timeout = 60  # Max 60 seconds to wait for proxy to be available
        
        print(f"📥 Fetching metadata for video {video_id}...")
        
        while retry_count < max_retries:
            try:
                # Wait for proxy with timeout
                wait_start = time.time()
                proxy = None
                check_count = 0
                while True:
                    proxy = self.proxy_pool.get_available_proxy()
                    if proxy:
                        print(f"   Got proxy {proxy} for {video_id} after {check_count} checks")
                        break
                    
                    check_count += 1
                    elapsed = time.time() - wait_start
                    if elapsed > proxy_wait_timeout:
                        # Print detailed pool status
                        all_stats = self.proxy_pool.get_all_stats()
                        print(f"⏱️  Timeout waiting {elapsed:.1f}s for available proxy for {video_id}")
                        print(f"   Pool status: {self.proxy_pool.get_pool_status()}")
                        for proxy_url, stats in all_stats.items():
                            print(f"   - {proxy_url}: {stats}")
                        return None, None
                    
                    # Every 60 checks (~6 seconds), print status
                    if check_count % 60 == 0:
                        all_stats = self.proxy_pool.get_all_stats()
                        print(f"   {video_id}: waiting for proxy (elapsed {elapsed:.1f}s)...")
                        for proxy_url, stats in all_stats.items():
                            print(f"     - {proxy_url}: {stats}")
                    
                    await asyncio.sleep(0.1)
                
                try:
                    # Fetch embed page
                    html = await self.http_client.fetch_embed_page(
                        video_id,
                        proxy=proxy if proxy != "__localhost__" else None
                    )
                    
                    # Extract JSON
                    json_data = MetadataExtractor.extract_json_from_html(html)
                    if not json_data:
                        raise ValueError("No JSON found in embed page")
                    
                    # Parse metadata
                    metadata = MetadataExtractor.extract_video_metadata(json_data, video_id)
                    if not metadata:
                        raise ValueError("Failed to parse metadata")
                    
                    # Validate
                    if not MetadataExtractor.validate_metadata(metadata):
                        raise ValueError("Metadata validation failed")
                    
                    self.proxy_pool.mark_proxy_success(proxy)
                    self.consecutive_failures = 0
                    print(f"✓ Metadata fetched for {video_id}")
                    return metadata, json.dumps(json_data)
                
                except Exception as e:
                    # Classify error and apply appropriate retry strategy
                    error_type = self._get_error_type(e)
                    retry_config = self.config['retry'].get(error_type, {})
                    max_attempts = retry_config.get('max_attempts', 3)
                    
                    self.proxy_pool.mark_proxy_failure(proxy, error_type)
                    self.consecutive_failures += 1
                    retry_count += 1
                    
                    error_msg = f"{type(e).__name__}: {str(e)[:100]}"
                    
                    if retry_count >= max_attempts:
                        print(f"❌ Failed to fetch metadata for {video_id} after {retry_count} retries ({error_type}): {error_msg}")
                        return None, None
                    
                    # Apply backoff before retry
                    delay, _ = await self._apply_retry_delay(error_type, retry_count, video_id)
                    print(f"⚠️  Retry {retry_count}/{max_attempts} for {video_id} ({error_type}): {error_msg}")
                    print(f"   Waiting {delay:.1f}s before retry...")
                    await asyncio.sleep(delay)
            
            except Exception as e:
                print(f"❌ Unexpected error fetching metadata for {video_id}: {e}")
                return None, None
        
        return None, None
    
    async def download_post(self, video_id: str, metadata: VideoMetadata, raw_json: Optional[str] = None) -> Optional[DownloadResult]:
        """
        Download media for a post after metadata has been fetched.
        
        Args:
            video_id: TikTok post ID
            metadata: Already-fetched metadata
            raw_json: Optional raw JSON metadata string
            
        Returns:
            DownloadResult with success/failure info
        """
        result = DownloadResult(
            post_id=video_id,
            status=DownloadStatus.DOWNLOADING,
            metadata=metadata,
            used_browser_fallback=False,
            raw_json=raw_json
        )
        
        retry_count = 0
        max_retries = self.config['http_client']['max_retries']
        
        while retry_count < max_retries:
            try:
                proxy = self.proxy_pool.get_available_proxy()
                if not proxy:
                    await asyncio.sleep(0.1)
                    continue
                
                proxy_url = proxy if proxy != "__localhost__" else None
                
                # Fetch JSON again to get download URLs
                html = await self.http_client.fetch_embed_page(video_id, proxy=proxy_url)
                json_data = MetadataExtractor.extract_json_from_html(html)
                
                if not json_data:
                    raise ValueError("No JSON in embed page")
                
                # Check if image or video post
                image_urls = MetadataExtractor.extract_image_urls(json_data, video_id)
                
                if image_urls:
                    # Image carousel post
                    metadata.is_image_post = True
                    metadata.image_count = len(image_urls)
                    
                    files = await MediaDownloader.download_images_parallel(
                        self.http_client,
                        video_id,
                        image_urls,
                        Path(self.output['images_dir']),
                        proxy=proxy_url,
                        max_concurrent=self.config['media_download'].get('max_concurrent_images', 3)
                    )
                    
                    result.files = files
                    result.success = all(f.success for f in files)
                
                else:
                    # Video post
                    video_url = MetadataExtractor.extract_video_urls(json_data, video_id)
                    if not video_url:
                        # No video URL found - this is an error only if there were also no images
                        print(f"⚠️  No images or video URL found for {video_id}")
                        raise ValueError("Post has neither images nor video")
                    
                    # Try to download video
                    file_result = await MediaDownloader.download_video(
                        self.http_client,
                        video_id,
                        video_url,
                        Path(self.output['videos_dir']),
                        proxy=proxy_url
                    )
                    
                    if not file_result.success and "HTTP 403" in (file_result.error or ""):
                        # Try browser fallback
                        if self.config['features'].get('use_browser_fallback', True):
                            print(f"🌐 Attempting browser fallback for {video_id}...")
                            embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"
                            src_url, video_data = await self.browser_handler.fetch_video_src_with_browser(
                                embed_url,
                                video_id
                            )
                            
                            if video_data:
                                filepath = Path(self.output['videos_dir']) / f"{video_id}.mp4"
                                with open(filepath, "wb") as f:
                                    f.write(video_data)
                                
                                file_result.filename = f"{video_id}.mp4"
                                file_result.success = True
                                file_result.error = None
                                result.used_browser_fallback = True
                                print(f"✓ Browser fallback succeeded for {video_id}")
                    
                    result.files = [file_result]
                    result.success = file_result.success
                
                if result.success:
                    self.proxy_pool.mark_proxy_success(proxy)
                    self.consecutive_failures = 0
                    result.status = DownloadStatus.SUCCESS
                    return result
                else:
                    self.proxy_pool.mark_proxy_failure(proxy, "download_failed")
                    retry_count += 1
            
            except Exception as e:
                # Classify error and apply appropriate retry strategy
                error_type = self._get_error_type(e)
                retry_config = self.config['retry'].get(error_type, {})
                max_attempts = retry_config.get('max_attempts', 3)
                
                if proxy:
                    self.proxy_pool.mark_proxy_failure(proxy, error_type)
                
                self.consecutive_failures += 1
                retry_count += 1
                
                if retry_count >= max_attempts:
                    result.error = f"{error_type}: {str(e)[:100]}"
                    result.status = DownloadStatus.FAILED
                    print(f"❌ Failed to download {video_id} after {retry_count} retries ({error_type}): {e}")
                    return result
                
                # Apply backoff before retry
                delay, _ = await self._apply_retry_delay(error_type, retry_count, video_id)
                print(f"⚠️  Retry {retry_count}/{max_attempts} for download {video_id} ({error_type})")
                print(f"   Waiting {delay:.1f}s before retry...")
                await asyncio.sleep(delay)
        
        result.error = "Max retries exceeded"
        result.status = DownloadStatus.FAILED
        return result
    
    async def scrape_batch(self, video_ids: List[str], on_result_callback=None) -> List[DownloadResult]:
        """
        Scrape videos in micro-batches to prevent memory buildup on large datasets.
        
        Process flow per batch:
        1. Fetch metadata for N posts
        2. Download media for those N posts
        3. Write results to CSV
        4. Clear memory before next batch
        
        Args:
            video_ids: List of video IDs to scrape
            on_result_callback: Optional callback function(result) called when each result completes
            
        Returns:
            List of all DownloadResult objects
        """
        batch_size = self.config.get('scraper', {}).get('batch_size', 10)
        all_results = []
        total = len(video_ids)
        
        print(f"\n📥 Processing {total} videos in batches of {batch_size}...")
        
        # Process videos in micro-batches
        for batch_start in range(0, total, batch_size):
            batch_end = min(batch_start + batch_size, total)
            batch_ids = video_ids[batch_start:batch_end]
            batch_num = (batch_start // batch_size) + 1
            total_batches = (total + batch_size - 1) // batch_size
            
            print(f"\n[Batch {batch_num}/{total_batches}] Processing {len(batch_ids)} videos (IDs {batch_start+1}-{batch_end})...")
            
            # Phase 1: Fetch metadata for this batch
            print(f"  ⏳ Fetching metadata...")
            metadata_dict = {}
            for video_id in batch_ids:
                metadata, raw_json = await self.fetch_metadata_for_post(video_id)
                if metadata:
                    metadata_dict[video_id] = (metadata, raw_json)
            
            print(f"  ✓ Metadata: {len(metadata_dict)}/{len(batch_ids)} succeeded")
            
            # Phase 2: Download media for this batch (in parallel)
            if metadata_dict:
                print(f"  ⏳ Downloading media...")
                download_tasks = {
                    video_id: asyncio.create_task(self.download_post(video_id, metadata, raw_json))
                    for video_id, (metadata, raw_json) in metadata_dict.items()
                }
                
                batch_results = []
                
                # Collect download results
                for video_id, task in download_tasks.items():
                    try:
                        result = await task
                        batch_results.append(result)
                        all_results.append(result)
                        if on_result_callback:
                            on_result_callback(result)
                    except Exception as e:
                        result = DownloadResult(
                            post_id=video_id,
                            status=DownloadStatus.FAILED,
                            error=str(e),
                            used_browser_fallback=False,
                            raw_json=None
                        )
                        batch_results.append(result)
                        all_results.append(result)
                        if on_result_callback:
                            on_result_callback(result)
                
                print(f"  ✓ Downloads: {len(batch_results)} completed")
            
            # Record failures for posts with no metadata in this batch
            for video_id in batch_ids:
                if video_id not in metadata_dict and not any(r.post_id == video_id for r in all_results):
                    result = DownloadResult(
                        post_id=video_id,
                        status=DownloadStatus.FAILED,
                        error="Failed to fetch metadata",
                        used_browser_fallback=False,
                        raw_json=None
                    )
                    all_results.append(result)
                    if on_result_callback:
                        on_result_callback(result)
            
            # Phase 3: Clear memory before next batch
            del metadata_dict
            del download_tasks
            print(f"  ✓ Batch complete. Memory cleared.")
        
        print(f"\n✓ Scraping complete: {len(all_results)}/{total} results")
        return all_results
    
    async def close(self):
        """Clean up resources."""
        await self.http_client.close()
        await self.browser_handler.close()
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
