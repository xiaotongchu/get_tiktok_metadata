"""
Main entry point for TikTok scraper (refactored clean version).

Usage:
    python main.py <input_csv> [--config config.yaml] [--output-dir .]
"""
import asyncio
import csv
import argparse
import logging
import sys
import traceback
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple, Set
from datetime import datetime

import pandas as pd
import yaml

from scraper import TikTokScraper
from models import DownloadResult


class LoggerWriter:
    """Redirect print statements to logger."""
    def __init__(self, logger: logging.Logger, level: int):
        self.logger = logger
        self.level = level
        self.buffer = ""
    
    def write(self, message: str):
        if message and message.strip():
            self.logger.log(self.level, message.rstrip())
    
    def flush(self):
        pass


def setup_logging(output_dir: Path) -> logging.Logger:
    """
    Set up comprehensive logging to write to both console and a file.
    
    Captures:
    - Explicit log messages
    - Uncaught exceptions
    - print() statements
    - All library logging
    
    Appends to existing log file with run separators.
    
    Args:
        output_dir: Output directory where terminal_log.txt will be created
        
    Returns:
        Logger object
    """
    logger = logging.getLogger('tiktok_scraper')
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # Prevent duplicate logging to root logger
    logger.handlers.clear()
    
    # Create formatters
    formatter = logging.Formatter('%(message)s')
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    
    # File handler - APPEND mode (mode='a')
    log_file = output_dir / 'terminal_log.txt'
    file_handler = logging.FileHandler(log_file, encoding='utf-8', mode='a')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    
    # Add handlers to logger
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    
    # Also configure root logger to capture all library logging
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    if not root_logger.handlers:
        root_logger.addHandler(console_handler)
        root_logger.addHandler(file_handler)
    
    # Suppress verbose httpx/httpcore request traces to just show application logs
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    
    # Redirect stdout and stderr to logger
    sys.stdout = LoggerWriter(logger, logging.INFO)
    sys.stderr = LoggerWriter(logger, logging.ERROR)
    
    # Add run separator with timestamp
    run_separator = f"\n{'='*80}\nRUN STARTED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*80}"
    logger.info(run_separator)
    
    # Capture uncaught exceptions
    def exception_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        
        logger.error("Uncaught exception:", exc_info=(exc_type, exc_value, exc_traceback))
        # Also print to stderr for visibility
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
    
    sys.excepthook = exception_handler
    
    return logger


def log_message(logger: logging.Logger, message: str) -> None:
    """Log a message to both console and file."""
    logger.info(message)


def resolve_output_path(
    output_dir: str,
    csv_filename: Optional[str] = None,
    config_path: str = "config.yaml",
    add_timestamp: bool = False
) -> tuple[Path, str]:
    """
    Resolve the output directory and CSV filename.
    
    Args:
        output_dir: Output directory (relative or absolute)
        csv_filename: Optional CSV filename override (if None, use config default)
        config_path: Path to config file for default values
        add_timestamp: Optional override to add timestamp to csv_filename
    
    Returns:
        Tuple of (output_dir_path, csv_output_path)
    """
    # Resolve output directory path (relative to script or absolute)
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Load config to get defaults if filename not provided
    if csv_filename is None or add_timestamp:
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            config_csv_filename = config.get('output', {}).get('csv_filename', 'metadata_output.csv')
            config_use_timestamp = config.get('output', {}).get('csv_timestamp', False)
        except Exception:
            config_csv_filename = 'metadata_output.csv'
            config_use_timestamp = False
        
        # Use provided filename, or fall back to config default
        if csv_filename is None:
            csv_filename = config_csv_filename
        
        # Check if we should add timestamp
        use_timestamp = add_timestamp or config_use_timestamp
    else:
        use_timestamp = False
    
    # Add timestamp if needed
    if use_timestamp:
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        name, ext = csv_filename.rsplit('.', 1) if '.' in csv_filename else (csv_filename, '')
        csv_filename = f"{name}_{timestamp}.{ext}" if ext else f"{name}_{timestamp}"
    
    csv_output_path = output_path / csv_filename
    
    return output_path, str(csv_output_path)


class StreamingCSVWriter:
    """
    Writes CSV results as they complete (streaming).
    
    Ensures results are written immediately to disk, so partial progress
    survives if the program crashes or stops unexpectedly.
    
    Handles updating existing post_id rows when re-scraping.
    """
    
    # Define expected fieldnames upfront to ensure all columns are created
    # even if early results fail
    EXPECTED_FIELDNAMES = [
        'post_id',
        'type',
        'downloaded',
        'download_time',
        'error_message',
        'status',
        'used_browser_fallback',
        # Metadata fields (from VideoMetadata.to_dict())
        'description',
        'author_name',
        'author_id',
        'author_verified',
        'create_time',
        'views',
        'likes',
        'shares',
        'comments',
        'sticker_texts',
        'is_ad',
        # Additional fields
        'raw_json',
    ]
    
    def __init__(self, output_path: str, posts_to_update: Set[str] = None):
        """
        Initialize CSV writer (appends to existing file if it exists).
        
        Args:
            output_path: Path to output CSV file
            posts_to_update: Deprecated, not used (kept for compatibility)
        """
        self.output_path = output_path
        self.file = None
        self.writer = None
        self.fieldnames = self.EXPECTED_FIELDNAMES
        self.writer_initialized = False
        self.total_written = 0
        self.file_exists = Path(output_path).exists()
        self.is_new_file = not self.file_exists
    
    def _initialize_writer(self) -> None:
        """Initialize the CSV writer and append to existing file or create new one."""
        if self.writer_initialized:
            return
        
        # Open file in append mode if it exists, otherwise write mode to create with header
        mode = 'a' if self.file_exists else 'w'
        self.file = open(self.output_path, mode, newline='', encoding='utf-8')
        self.writer = csv.DictWriter(self.file, fieldnames=self.fieldnames)
        
        # Write header only if creating a new file
        if not self.file_exists:
            self.writer.writeheader()
        
        self.writer_initialized = True
        self.file.flush()
    
    def write_result(self, result: DownloadResult) -> None:
        """
        Write a single result to CSV immediately.
        
        Args:
            result: DownloadResult to write
        """
        # Initialize writer on first write
        if not self.writer_initialized:
            self._initialize_writer()
        
        # Set download time if not already set
        if result.download_time is None:
            result.download_time = datetime.now()
        
        # Determine post type
        if result.metadata:
            post_type = "image" if result.metadata.is_image_post else "video"
        else:
            post_type = "unknown"
        
        # Convert result to row dict
        row = {
            'post_id': result.post_id,
            'type': post_type,
            'downloaded': result.success,
            'download_time': result.download_time.isoformat(),
            'error_message': result.error or '',
            'status': result.status.value,
            'used_browser_fallback': result.used_browser_fallback,
            # Initialize all expected metadata fields with empty strings if missing
            'description': '',
            'author_name': '',
            'author_id': '',
            'author_verified': '',
            'create_time': '',
            'views': '',
            'likes': '',
            'shares': '',
            'comments': '',
            'sticker_texts': '',
            'is_ad': '',
            'raw_json': '',
        }
        
        if result.metadata:
            row.update(result.metadata.to_dict())
        
        # Add raw json last to improve readability
        if result.raw_json:
            row['raw_json'] = result.raw_json
        
        # Write the row
        self.writer.writerow(row)
        self.file.flush()  # Force write to disk immediately
        self.total_written += 1
        
        # Print progress
        status_icon = "✓" if result.success else "✗"
        print(f"   {status_icon} [{self.total_written}] Saved: {result.post_id}")
    
    def close(self) -> None:
        """Close the CSV file."""
        if self.file:
            self.file.close()
    
    async def __aenter__(self):
        """Context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


async def read_input_csv(csv_path: str, logger: logging.Logger) -> List[str]:
    """
    Read video IDs from input CSV.
    
    Expected columns:
    - post_ID or id: Video ID
    - Link: TikTok URL (optional)
    - type: "img" for image posts (optional)
    
    Args:
        csv_path: Path to input CSV file
        logger: Logger object
        
    Returns:
        List of unique video IDs
    """
    df = pd.read_csv(csv_path)
    
    # Find ID column
    id_column = None
    for col in ['post_ID', 'id', 'post_id']:
        if col in df.columns:
            id_column = col
            break
    
    if not id_column:
        raise ValueError(f"Could not find post ID column. Columns: {df.columns.tolist()}")
    
    # Get unique IDs as strings
    unique_ids = [str(vid) for vid in df[id_column].unique()]
    log_message(logger, f"📋 Loaded {len(unique_ids)} unique video IDs from {csv_path}")
    
    return unique_ids


async def get_scraping_candidates(
    output_path: str,
    input_video_ids: List[str],
    logger: logging.Logger
) -> Tuple[List[str], Set[str]]:
    """
    Determine which videos to scrape.
    
    Logic:
    - New videos (not in output): Scrape
    - Existing videos (in output): Skip (resume by appending to file)
    
    Args:
        output_path: Path to output CSV file
        input_video_ids: List of video IDs from input CSV
        logger: Logger object
        
    Returns:
        Tuple of (videos_to_process, posts_to_update)
        - videos_to_process: List of video IDs to scrape
        - posts_to_update: Empty set (no updates needed with append-only mode)
    """
    if not Path(output_path).exists():
        log_message(logger, f"📌 No existing output file found, all posts will be scraped")
        return input_video_ids, set()
    
    try:
        df = pd.read_csv(output_path)
        
        if df.empty or 'post_id' not in df.columns:
            return input_video_ids, set()
        
        # Convert to strings for comparison
        existing_post_ids = set(str(pid) for pid in df['post_id'].unique())
        new_video_ids = [vid for vid in input_video_ids if vid not in existing_post_ids]
        
        log_message(logger, f"📌 Found {len(existing_post_ids)} existing posts in {output_path}")
        if len(new_video_ids) > 0:
            log_message(logger, f"   {len(new_video_ids)} new posts to scrape")
        
        return new_video_ids, set()
        
    except Exception as e:
        log_message(logger, f"⚠️  Could not load existing output: {e}")
        return input_video_ids, set()


async def load_already_scraped(output_path: str) -> set:
    """
    Load already-scraped post IDs from existing output CSV.
    
    Args:
        output_path: Path to output CSV file
        
    Returns:
        Set of post_ids already processed
    """
    if not Path(output_path).exists():
        return set()
    
    try:
        df = pd.read_csv(output_path)
        if df.empty or 'post_id' not in df.columns:
            return set()
        
        already_scraped = set(str(pid) for pid in df['post_id'].unique())
        print(f"📌 Found {len(already_scraped)} already-scraped posts in {output_path}")
        return already_scraped
    except Exception as e:
        print(f"⚠️  Could not load existing output: {e}")
        return set()


async def get_results_summary(output_path: str, logger: logging.Logger) -> None:
    """
    Print summary of results from completed CSV.
    
    Args:
        output_path: Path to output CSV file
        logger: Logger object
    """
    try:
        if not Path(output_path).exists():
            log_message(logger, f"⚠️  Output file not created: {output_path}")
            return
        
        df = pd.read_csv(output_path)
        if df.empty:
            log_message(logger, f"⚠️  Output file is empty: {output_path}")
            return
        
        successful = int((df['downloaded'] == True).sum())
        failed = len(df) - successful
        
        log_message(logger, f"📊 Results written to {output_path}")
        log_message(logger, f"   {successful}/{len(df)} successful downloads")
        if failed > 0:
            log_message(logger, f"   {failed} failed")
    except Exception as e:
        log_message(logger, f"⚠️  Error reading results: {e}")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='TikTok metadata scraper (refactored clean version)\n'
                    'Downloads TikTok videos/images and extracts metadata.'
    )
    
    parser.add_argument('input_csv', help='Input CSV file with video IDs')
    parser.add_argument('--config', default='config.yaml', help='Configuration file')
    parser.add_argument('--output-dir', default='../output', help='Output directory (relative or absolute path)')
    parser.add_argument('--csv-filename', default=None, help='CSV output filename (overrides config default)')
    parser.add_argument('--timestamp', action='store_true', help='Add datetime timestamp to CSV filename')
    
    args = parser.parse_args()
    
    # Validate input
    if not Path(args.input_csv).exists():
        print(f"❌ Input file not found: {args.input_csv}")
        return
    
    if not Path(args.config).exists():
        print(f"❌ Config file not found: {args.config}")
        return
    
    # Resolve output paths (never add timestamp to metadata_output.csv)
    output_dir, csv_output_path = resolve_output_path(
        args.output_dir,
        csv_filename=args.csv_filename,
        config_path=args.config,
        add_timestamp=False  # Always use consistent filename for deduplication
    )
    
    # Set up logging to file and console
    logger = setup_logging(output_dir)
    
    log_message(logger, f"🚀 Starting TikTok scraper")
    log_message(logger, f"   Input: {args.input_csv}")
    log_message(logger, f"   Config: {args.config}")
    log_message(logger, f"   Output dir: {output_dir}")
    log_message(logger, f"   CSV output: {csv_output_path}")
    log_message(logger, "")
    
    try:
        # Read input
        video_ids = await read_input_csv(args.input_csv, logger)
        log_message(logger, "")
        
        # Get scraping candidates (new + rescrape)
        videos_to_process, posts_to_update = await get_scraping_candidates(
            csv_output_path, video_ids, logger
        )
        
        if videos_to_process:
            log_message(logger, f"📊 Total posts to process: {len(videos_to_process)}/{len(video_ids)} (new posts)")
            log_message(logger, "")
        else:
            log_message(logger, f"✓ All {len(video_ids)} posts already in output file!")
            log_message(logger, f"   Remove or rename {csv_output_path} to start fresh.")
            return
        
        # Run scraper with streaming CSV writer
        csv_writer = StreamingCSVWriter(csv_output_path, posts_to_update=posts_to_update)
        try:
            async with TikTokScraper(args.config, output_dir=str(output_dir)) as scraper:
                log_message(logger, f"📍 {scraper.proxy_pool.get_pool_status()}")
                log_message(logger, "")
                
                # Pass CSV writer callback for real-time result writing
                results = await scraper.scrape_batch(
                    videos_to_process,
                    on_result_callback=csv_writer.write_result
                )
        finally:
            # Always close CSV writer, even if scraper fails
            csv_writer.close()
        
        log_message(logger, "")
        
        # Print summary
        await get_results_summary(csv_output_path, logger)
        
        log_message(logger, "")
        log_message(logger, "✓ Done!")
    
    except Exception as e:
        log_message(logger, f"❌ Error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
