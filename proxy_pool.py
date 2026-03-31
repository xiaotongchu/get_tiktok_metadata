"""
Intelligent proxy pool management with O(1) availability checks and exponential backoff.
Replaces the linear O(n) search in the original implementation.
"""
import time
from typing import Optional, List, Dict
from collections import deque
from models import ProxyStats


class ProxyPool:
    """
    Manages a pool of proxies with intelligent availability tracking,
    exponential backoff, and circuit breaker pattern.
    
    O(1) availability lookup vs O(n) in original implementation.
    """
    
    def __init__(self, proxy_list: List[str], config: Dict):
        """
        Initialize proxy pool.
        
        Args:
            proxy_list: List of proxy URLs or "__localhost__"
            config: Proxy configuration dict with throttle_seconds, backoff factors, etc.
        """
        self.config = config
        self.proxy_stats: Dict[str, ProxyStats] = {}
        self.available_queue: deque = deque()  # Queue of available proxies
        
        # Initialize all proxies
        for proxy_url in proxy_list:
            stats = ProxyStats(proxy_url=proxy_url)
            self.proxy_stats[proxy_url] = stats
            self.available_queue.append(proxy_url)
    
    def get_available_proxy(self) -> Optional[str]:
        """
        Get next available proxy in O(1) time by checking available_queue.
        
        Returns:
            Proxy URL or None if no proxy available
        """
        current_time = time.time()
        
        # Drain queue, checking each proxy until we find an available one
        checked = 0
        while self.available_queue and checked < len(self.available_queue) * 2:
            proxy_url = self.available_queue.popleft()
            stats = self.proxy_stats[proxy_url]
            
            if stats.is_available(current_time):
                # Mark as busy and return
                stats.busy = True
                return proxy_url
            else:
                # Not available yet, put back in queue
                # Debug: show why not available
                reasons = []
                if stats.busy:
                    reasons.append("busy")
                if stats.is_broken:
                    reasons.append("broken")
                if stats.next_available > current_time:
                    reasons.append(f"throttle in {stats.next_available - current_time:.2f}s")
                if stats.cooldown_until > current_time:
                    reasons.append(f"cooldown in {stats.cooldown_until - current_time:.2f}s")
                # Uncomment for very verbose logging:
                # print(f"   Proxy {proxy_url} not available: {', '.join(reasons)}")
                
                self.available_queue.append(proxy_url)
                checked += 1
        
        return None
    
    def release_proxy(self, proxy_url: str) -> None:
        """
        Mark proxy as no longer busy.
        
        Args:
            proxy_url: The proxy to release
        """
        if proxy_url not in self.proxy_stats:
            return
        
        stats = self.proxy_stats[proxy_url]
        stats.busy = False
        stats.current_url = None
        
        # Add back to available queue
        self.available_queue.append(proxy_url)
    
    def mark_proxy_success(self, proxy_url: str) -> None:
        """
        Mark a proxy request as successful and reset backoff.
        
        Args:
            proxy_url: The proxy that succeeded
        """
        if proxy_url not in self.proxy_stats:
            return
        
        stats = self.proxy_stats[proxy_url]
        stats.success_count += 1
        
        # Reset backoff on success
        stats.reset()
        
        # Release proxy (set busy=False so it can be used for next request)
        stats.busy = False
        
        # Set next_available to throttle_seconds in the future
        throttle = self.config.get('throttle_seconds', 1)
        stats.next_available = time.time() + throttle
        stats.last_used = time.time()
        
        # Put proxy back in available queue
        self.available_queue.append(proxy_url)
    
    def mark_proxy_failure(self, proxy_url: str, error_type: str = "connection") -> None:
        """
        Mark a proxy request as failed and apply exponential backoff.
        
        Args:
            proxy_url: The proxy that failed
            error_type: Type of error (connection, timeout, http_error, etc.)
        """
        if proxy_url not in self.proxy_stats:
            return
        
        stats = self.proxy_stats[proxy_url]
        
        # Calculate new cooldown (exponential backoff)
        backoff_factor = self.config.get('backoff_factor', 2.0)
        new_cooldown = stats.cooldown_duration * backoff_factor
        
        stats.mark_failure(new_cooldown)
        
        # Release proxy (set busy=False so it can be retried)
        stats.busy = False
        
        # Check if proxy should be marked "broken"
        failure_threshold = self.config.get('failure_threshold', 5)
        if stats.failure_count >= failure_threshold:
            stats.is_broken = True
            print(f"⚠️  Proxy {proxy_url} marked broken after {stats.failure_count} failures")
        
        print(f"📍 Proxy {proxy_url}: failed (attempt {stats.failure_count}), "
              f"cooldown {stats.cooldown_duration:.1f}s")
        
        # Put proxy back in available queue (will check is_available() before use)
        self.available_queue.append(proxy_url)
    
    def reset_proxy(self, proxy_url: str) -> None:
        """
        Manually reset a proxy's state (e.g., after maintenance).
        
        Args:
            proxy_url: The proxy to reset
        """
        if proxy_url not in self.proxy_stats:
            return
        
        stats = self.proxy_stats[proxy_url]
        stats.reset()
        stats.next_available = time.time()
        stats.last_used = None
        print(f"🔄 Proxy {proxy_url} reset")
    
    def get_all_stats(self) -> Dict[str, dict]:
        """Get statistics for all proxies (for monitoring)."""
        current_time = time.time()
        return {
            proxy_url: {
                'available': stats.is_available(current_time),
                'busy': stats.busy,
                'failures': stats.failure_count,
                'successes': stats.success_count,
                'is_broken': stats.is_broken,
                'throttle_until': max(0, stats.next_available - current_time),  # Seconds until usable
                'cooldown_until': max(0, stats.cooldown_until - current_time),  # Seconds of remaining backoff
            }
            for proxy_url, stats in self.proxy_stats.items()
        }
    
    def any_proxy_available_soon(self, timeout_seconds: float = 600) -> bool:
        """
        Check if any proxy will be available within timeout_seconds.
        Used to abort if we're waiting too long.
        
        Args:
            timeout_seconds: Maximum seconds to wait
            
        Returns:
            True if at least one proxy will be available within timeout
        """
        current_time = time.time()
        deadline = current_time + timeout_seconds
        
        for stats in self.proxy_stats.values():
            # Check when proxy will be available
            earliest_available = max(stats.next_available, stats.cooldown_until)
            if earliest_available <= deadline:
                return True
        
        return False
    
    def get_pool_status(self) -> str:
        """Get human-readable status of proxy pool."""
        total = len(self.proxy_stats)
        healthy = sum(1 for s in self.proxy_stats.values() if s.failure_count == 0 and not s.is_broken)
        broken = sum(1 for s in self.proxy_stats.values() if s.is_broken)
        busy = sum(1 for s in self.proxy_stats.values() if s.busy)
        
        return f"Proxies: {healthy}/{total} healthy, {broken} broken, {busy} busy"
