"""
Data models for TikTok scraper using Pydantic for type safety and validation.
"""
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime


class DownloadStatus(str, Enum):
    """Status of a download operation."""
    PENDING = "pending"
    FETCHING_METADATA = "fetching_metadata"
    DOWNLOADING = "downloading"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class VideoStats:
    """Stats for a TikTok video."""
    views: int = 0
    likes: int = 0
    shares: int = 0
    comments: int = 0


@dataclass
class VideoMetadata:
    """Core metadata extracted from a TikTok post."""
    post_id: str
    description: str
    author_name: str
    author_id: str
    create_time: str
    stats: VideoStats
    author_verified: Optional[bool] = None
    is_image_post: bool = False
    image_count: int = 0
    sticker_texts: str = ""  # Concatenated sticker texts from stickerTextList
    is_ad: Optional[bool] = None  # Whether the post is an advertisement
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for CSV output."""
        return {
            'post_id': self.post_id,
            'description': self.description,
            'author_name': self.author_name,
            'author_id': self.author_id,
            'author_verified': self.author_verified,
            'create_time': self.create_time,
            'views': self.stats.views,
            'likes': self.stats.likes,
            'shares': self.stats.shares,
            'comments': self.stats.comments,
            'sticker_texts': self.sticker_texts,
            'is_ad': self.is_ad,
        }


@dataclass
class DownloadedFile:
    """Represents a single downloaded file (video or image)."""
    filename: str
    success: bool
    image_index: Optional[int] = None  # For image carousels
    error: Optional[str] = None


@dataclass
class DownloadResult:
    """Result of attempting to download a single post."""
    post_id: str
    status: DownloadStatus = DownloadStatus.PENDING
    success: bool = False
    metadata: Optional[VideoMetadata] = None
    files: List[DownloadedFile] = field(default_factory=list)
    error: Optional[str] = None
    used_browser_fallback: bool = False  # Indicates if browser fallback was used
    raw_json: Optional[str] = None  # Raw JSON metadata as string
    download_time: Optional[datetime] = None  # Timestamp when result was recorded
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for CSV output."""
        # Determine post type
        if self.metadata:
            post_type = "image" if self.metadata.is_image_post else "video"
        else:
            post_type = "unknown"
        
        result = {
            'post_id': self.post_id,
            'type': post_type,
            'downloaded': self.success,
            'error_message': self.error or '',
            'raw_json': self.raw_json or '',
            'download_time': self.download_time.isoformat() if self.download_time else '',
        }
        if self.metadata:
            result.update(self.metadata.to_dict())
        return result


@dataclass
class ProxyStats:
    """Statistics and state for a proxy in the pool."""
    proxy_url: str
    busy: bool = False
    current_url: Optional[str] = None
    failure_count: int = 0
    success_count: int = 0
    last_used: Optional[float] = None
    next_available: float = 0.0  # Unix timestamp when proxy can be used again
    cooldown_until: float = 0.0  # Unix timestamp when cooldown expires
    cooldown_duration: float = 2.0  # Current cooldown (exponential backoff)
    is_broken: bool = False  # Circuit breaker: too many failures
    broken_since: Optional[float] = None  # Unix timestamp when marked broken
    
    def reset(self) -> None:
        """Reset proxy state after successful use."""
        self.failure_count = 0
        self.cooldown_duration = 2.0
        self.is_broken = False
        self.broken_since = None
    
    def mark_failure(self, new_cooldown: float) -> None:
        """Mark a failure and apply cooldown."""
        self.failure_count += 1
        self.cooldown_duration = min(new_cooldown, 300.0)  # Cap at 5 minutes
        self.cooldown_until = datetime.now().timestamp() + self.cooldown_duration
        
    def is_available(self, current_time: float) -> bool:
        """Check if proxy is available for use."""
        return (
            not self.busy
            and not self.is_broken
            and self.next_available <= current_time
            and self.cooldown_until <= current_time
        )
