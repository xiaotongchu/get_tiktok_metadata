"""
Media download handler for videos and images with parallel image support.
"""
import asyncio
from typing import Optional, List, Tuple
from pathlib import Path
from models import DownloadedFile


class MediaDownloader:
    """
    Handles downloading and saving video and image files.
    
    Improvements:
    - Parallel image downloads (via asyncio.gather)
    - Structured file extension detection
    - Clear error tracking per file
    """
    
    CONTENT_TYPE_TO_EXTENSION = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "video/mp4": "mp4",
        "video/quicktime": "mov",
    }
    
    @staticmethod
    def get_file_extension(content_type: str, default: str = "jpg") -> str:
        """
        Determine file extension from Content-Type header.
        
        Args:
            content_type: HTTP Content-Type header value
            default: Default extension if not recognized
            
        Returns:
            File extension (without dot)
        """
        content_type = (content_type or "").lower()
        
        # Direct lookup
        for ct, ext in MediaDownloader.CONTENT_TYPE_TO_EXTENSION.items():
            if ct in content_type:
                return ext
        
        # Fallback: try to extract subtype
        if "/" in content_type:
            subtype = content_type.split("/")[-1]
            subtype = subtype.split(";")[0].strip()
            if subtype and len(subtype) <= 4:  # Sanity check
                return subtype
        
        return default
    
    @staticmethod
    async def download_video(
        http_client,
        video_id: str,
        download_url: str,
        output_path: Path,
        proxy: Optional[str] = None
    ) -> DownloadedFile:
        """
        Download a video file.
        
        Args:
            http_client: TikTokHTTPClient instance
            video_id: Video ID (for logging)
            download_url: URL to download from
            output_path: Path to save video file
            proxy: Proxy URL to use
            
        Returns:
            DownloadedFile result
        """
        try:
            content = await http_client.download_media(
                download_url,
                media_type="video",
                proxy=proxy
            )
            
            if content is None:
                return DownloadedFile(
                    filename="",
                    success=False,
                    error="HTTP 403 - consider browser fallback"
                )
            
            filename = f"{video_id}.mp4"
            filepath = output_path / filename
            
            with open(filepath, "wb") as f:
                f.write(content)
            
            print(f"✓ Downloaded video: {filename}")
            
            return DownloadedFile(filename=filename, success=True)
        
        except Exception as e:
            error_msg = f"Failed to download video: {e}"
            print(f"✗ {error_msg}")
            return DownloadedFile(filename="", success=False, error=error_msg)
    
    @staticmethod
    async def download_images_parallel(
        http_client,
        video_id: str,
        image_urls: List[str],
        output_path: Path,
        proxy: Optional[str] = None,
        max_concurrent: int = 3
    ) -> List[DownloadedFile]:
        """
        Download multiple images in parallel (key improvement over sequential).
        
        Args:
            http_client: TikTokHTTPClient instance
            video_id: Video ID (for logging)
            image_urls: List of image URLs to download
            output_path: Directory to save images
            proxy: Proxy URL to use
            max_concurrent: Maximum concurrent downloads
            
        Returns:
            List of DownloadedFile results
        """
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def download_single_image(index: int, url: str) -> DownloadedFile:
            async with semaphore:
                try:
                    content = await http_client.download_media(
                        url,
                        media_type="image",
                        proxy=proxy
                    )
                    
                    if content is None:
                        return DownloadedFile(
                            filename="",
                            image_index=index,
                            success=False,
                            error="HTTP 403"
                        )
                    
                    # Detect extension from content
                    content_type = await http_client.get_content_type(url, proxy)
                    ext = MediaDownloader.get_file_extension(content_type, default="jpg")
                    
                    filename = f"{video_id}_{index}.{ext}"
                    filepath = output_path / filename
                    
                    with open(filepath, "wb") as f:
                        f.write(content)
                    
                    print(f"✓ Downloaded image: {filename}")
                    
                    return DownloadedFile(
                        filename=filename,
                        image_index=index,
                        success=True
                    )
                
                except Exception as e:
                    error_msg = f"Failed to download image {index}: {e}"
                    print(f"✗ {error_msg}")
                    return DownloadedFile(
                        filename="",
                        image_index=index,
                        success=False,
                        error=error_msg
                    )
        
        # Download all images concurrently
        tasks = [
            download_single_image(idx, url)
            for idx, url in enumerate(image_urls)
        ]
        
        return await asyncio.gather(*tasks)
