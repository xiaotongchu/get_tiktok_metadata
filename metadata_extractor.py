"""
Metadata extraction from TikTok embed pages.
Consolidates fragile multi-selector logic from original implementation.
"""
import json
from typing import Optional, List, Tuple, Dict, Any
from bs4 import BeautifulSoup
from models import VideoMetadata, VideoStats


class MetadataExtractor:
    """
    Extracts TikTok metadata from embed page HTML.
    
    Improvements:
    - Consolidated JSON selector logic (try SIGI_STATE, then __UNIVERSAL_DATA_FOR_REHYDRATION__, etc.)
    - Handles both video and image carousel posts
    - Cleaner parsing with explicit error messages
    """
    
    # JSON script tags to try in priority order
    JSON_SCRIPT_SELECTORS = [
        "SIGI_STATE",
        "__UNIVERSAL_DATA_FOR_REHYDRATION__",
        "__FRONTITY_CONNECT_STATE__",
    ]
    
    @staticmethod
    def extract_json_from_html(html: str) -> Optional[Dict[str, Any]]:
        """
        Extract embedded JSON from HTML script tags.
        Tries multiple selectors in priority order.
        
        Args:
            html: HTML content of embed page
            
        Returns:
            Parsed JSON dict, or None if not found
        """
        soup = BeautifulSoup(html, "html.parser")
        
        for selector in MetadataExtractor.JSON_SCRIPT_SELECTORS:
            script = soup.select_one(f"script#{selector}")
            
            if not script:
                continue
            
            try:
                text = script.text or (script.contents[0] if script.contents else None)
                if text:
                    return json.loads(text)
            except json.JSONDecodeError:
                continue
        
        return None
    
    @staticmethod
    def extract_video_metadata(json_data: Dict[str, Any], video_id: str) -> Optional[VideoMetadata]:
        """
        Extract core metadata from embed JSON.
        
        Args:
            json_data: Parsed JSON from embed page
            video_id: The video ID being processed
            
        Returns:
            VideoMetadata object, or None if extraction failed
        """
        try:
            # First try to get video data from standard nested structure
            video_data = MetadataExtractor._get_video_data(json_data)
            
            if not video_data:
                print(f"⚠️  Could not find video data in JSON for {video_id}")
                return None
            
            # Extract from itemInfos (new format)
            item_infos = video_data.get("itemInfos", {})
            
            # Extract description
            description = item_infos.get("text", "")
            
            # Extract create time
            create_time = str(item_infos.get("createTime", ""))
            
            # Extract author info from authorInfos
            author_infos = video_data.get("authorInfos", {})
            author_name = author_infos.get("nickName", "")
            author_id = author_infos.get("userId", "")
            author_verified = author_infos.get("verified", None)
            
            # Extract stats from itemInfos (direct fields)
            stats = VideoStats(
                views=item_infos.get("playCount", 0),
                likes=item_infos.get("diggCount", 0),
                shares=item_infos.get("shareCount", 0),
                comments=item_infos.get("commentCount", 0),
            )
            
            # Extract sticker texts
            sticker_texts = MetadataExtractor._extract_sticker_texts(video_data)
            
            # Extract is_ad flag
            is_ad = item_infos.get("isAd", None)
            
            # Extract basic metadata
            metadata = VideoMetadata(
                post_id=video_id,
                description=description,
                author_name=author_name,
                author_id=author_id,
                author_verified=author_verified,
                create_time=create_time,
                stats=stats,
                is_image_post=False,  # Determined later
                image_count=0,
                sticker_texts=sticker_texts,
                is_ad=is_ad,
            )
            
            return metadata
        
        except (KeyError, TypeError, AttributeError) as e:
            print(f"⚠️  Error extracting metadata for {video_id}: {e}")
            return None
    
    @staticmethod
    def extract_video_urls(json_data: Dict[str, Any], video_id: str) -> Optional[str]:
        """
        Extract download URL for a video post.
        
        Args:
            json_data: Parsed JSON from embed page
            video_id: Video ID
            
        Returns:
            Download URL, or None if not found
        """
        try:
            video_data = MetadataExtractor._get_video_data(json_data)
            if not video_data:
                return None
            
            item_infos = video_data.get("itemInfos", {})
            video_urls = item_infos.get("video", {}).get("urls", [])
            
            if video_urls:
                return video_urls[0]
            else:
                print(f"⚠️  No video URLs found for {video_id}")
                return None
        
        except (KeyError, TypeError, IndexError) as e:
            print(f"⚠️  Error extracting video URL for {video_id}: {e}")
            return None
    
    @staticmethod
    def extract_image_urls(json_data: Dict[str, Any], video_id: str) -> Optional[List[str]]:
        """
        Extract image URLs for image carousel posts.
        
        Args:
            json_data: Parsed JSON from embed page
            video_id: Video ID
            
        Returns:
            List of image URLs (in order), or None if not an image post
        """
        try:
            video_data = MetadataExtractor._get_video_data(json_data)
            if not video_data:
                return None
            
            image_post_info = video_data.get("imagePostInfo", {})
            display_images = image_post_info.get("displayImages", [])
            
            if not display_images:
                # Not an image post
                return None
            
            # Extract URL from each image object
            image_urls = []
            for img_obj in display_images:
                url_list = img_obj.get("urlList", [])
                if url_list:
                    image_urls.append(url_list[0])  # Use first URL (best quality)
            
            if image_urls:
                return image_urls
            else:
                print(f"⚠️  Image post {video_id} found but no valid URLs in urlList")
                return None
        
        except (KeyError, TypeError, AttributeError) as e:
            # Silently return None - will check for video as fallback
            return None
    
    @staticmethod
    def _extract_sticker_texts(video_data: Dict[str, Any]) -> str:
        """
        Extract all sticker texts from stickerTextList.
        Replaces newlines with literal \n and joins multiple texts with |.
        
        Args:
            video_data: Video data dictionary
            
        Returns:
            Concatenated sticker texts separated by "|", or empty string if none found
        """
        try:
            sticker_text_list = video_data.get("stickerTextList", [])
            if not sticker_text_list:
                return ""
            
            all_texts = []
            for sticker in sticker_text_list:
                sticker_texts = sticker.get("stickerText", [])
                for text in sticker_texts:
                    if text:  # Skip empty texts
                        # Replace actual newlines with literal \n
                        normalized_text = text.replace('\n', '\\n').replace('\r\n', '\\n')
                        all_texts.append(normalized_text)
            
            return "|".join(all_texts)
        
        except (KeyError, TypeError, AttributeError):
            return ""
    
    @staticmethod
    def _get_video_data(json_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Navigate to video data in various JSON structures.
        Handles multiple possible JSON layouts from TikTok.
        
        Args:
            json_data: Root JSON object
            
        Returns:
            Video data dict, or None if not found
        """
        # Try structure 1: __DEFAULT_SCOPE__.webapp.video-detail
        try:
            if "__DEFAULT_SCOPE__" in json_data:
                detail = json_data["__DEFAULT_SCOPE__"].get("webapp.video-detail", {})
                if "itemInfo" in detail:
                    return detail["itemInfo"]["itemStruct"]
        except (KeyError, TypeError):
            pass
        
        # Try structure 2: ItemModule (redirect from above)
        try:
            if "ItemModule" in json_data:
                items = json_data["ItemModule"]
                # Get first (and usually only) item
                for item_id, item in items.items():
                    return item
        except (KeyError, TypeError):
            pass
        
        # Try structure 3: Top-level item data (new format)
        # If json_data itself contains itemInfos, authorInfos, etc., it might be the item
        if "itemInfos" in json_data or "authorInfos" in json_data:
            return json_data
        
        # Try structure 4: source.data[0].videoData
        try:
            if "source" in json_data:
                source = json_data["source"]
                if "data" in source:
                    data_list = source["data"]
                    if isinstance(data_list, dict):
                        first_data = list(data_list.values())[0]
                    else:
                        first_data = data_list[0]
                    return first_data.get("videoData")
        except (KeyError, TypeError, IndexError):
            pass
        
        return None
    
    @staticmethod
    def validate_metadata(metadata: VideoMetadata) -> bool:
        """
        Validate that metadata is complete and useful.
        
        Args:
            metadata: Metadata to validate
            
        Returns:
            True if metadata is valid
        """
        # Check for minimum required fields
        if not metadata.post_id:
            return False
        
        # Check if stats are populated (indicator of complete data)
        if metadata.create_time == "0":
            # Post may require login to view
            return False
        
        return True
