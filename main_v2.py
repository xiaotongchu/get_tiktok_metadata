"""
Main entry point for TikTok scraper (refactored clean version).

Usage:
    python main.py <input_csv> [--config config.yaml] [--output-dir .]
"""
import asyncio
import csv
import argparse
from pathlib import Path
from typing import List, Optional, Dict, Any

import pandas as pd

from scraper import TikTokScraper
from models import DownloadResult


class StreamingCSVWriter:
    """
    Writes CSV results as they complete (streaming).
    
    Ensures results are written immediately to disk, so partial progress
    survives if the program crashes or stops unexpectedly.
    """
    
    def __init__(self, output_path: str):
        """
        Initialize CSV writer (creates file with headers).
        
        Args:
            output_path: Path to output CSV file
        """
        self.output_path = output_path
        self.file = None
        self.writer = None
        self.fieldnames = None
        self.writer_initialized = False
        self.total_written = 0
    
    def write_result(self, result: DownloadResult) -> None:
        """
        Write a single result to CSV immediately.
        
        Args:
            result: DownloadResult to write
        """
        # Convert result to row dict
        row = {
            'post_id': result.post_id,
            'downloaded': result.success,
            'error_message': result.error or '',
            'status': result.status.value,
        }
        
        if result.metadata:
            row.update(result.metadata.to_dict())
        
        # Initialize writer on first write (we now know fieldnames)
        if not self.writer_initialized:
            self.fieldnames = list(row.keys())
            self.file = open(self.output_path, 'w', newline='', encoding='utf-8')
            self.writer = csv.DictWriter(self.file, fieldnames=self.fieldnames)
            self.writer.writeheader()
            self.writer_initialized = True
            self.file.flush()
        
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


async def read_input_csv(csv_path: str) -> List[str]:
    """
    Read video IDs from input CSV.
    
    Expected columns:
    - post_ID or id: Video ID
    - Link: TikTok URL (optional)
    - type: "img" for image posts (optional)
    
    Args:
        csv_path: Path to input CSV file
        
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
    print(f"📋 Loaded {len(unique_ids)} unique video IDs from {csv_path}")
    
    return unique_ids


async def get_results_summary(output_path: str) -> None:
    """
    Print summary of results from completed CSV.
    
    Args:
        output_path: Path to output CSV file
    """
    try:
        if not Path(output_path).exists():
            print(f"⚠️  Output file not created: {output_path}")
            return
        
        df = pd.read_csv(output_path)
        if df.empty:
            print(f"⚠️  Output file is empty: {output_path}")
            return
        
        successful = int((df['downloaded'] == True).sum())
        failed = len(df) - successful
        
        print(f"📊 Results written to {output_path}")
        print(f"   {successful}/{len(df)} successful downloads")
        if failed > 0:
            print(f"   {failed} failed")
    except Exception as e:
        print(f"⚠️  Error reading results: {e}")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='TikTok metadata scraper (refactored clean version)\n'
                    'Downloads TikTok videos/images and extracts metadata.'
    )
    parser.add_argument('input_csv', help='Input CSV file with video IDs')
    parser.add_argument('--config', default='config.yaml', help='Configuration file')
    parser.add_argument('--output-csv', default='metadata_output.csv', help='Output CSV file')
    
    args = parser.parse_args()
    
    # Validate input
    if not Path(args.input_csv).exists():
        print(f"❌ Input file not found: {args.input_csv}")
        return
    
    if not Path(args.config).exists():
        print(f"❌ Config file not found: {args.config}")
        return
    
    print(f"🚀 Starting TikTok scraper")
    print(f"   Input: {args.input_csv}")
    print(f"   Config: {args.config}")
    print(f"   Output: {args.output_csv}")
    print()
    
    try:
        # Read input
        video_ids = await read_input_csv(args.input_csv)
        print()
        
        # Run scraper with streaming CSV writer
        csv_writer = StreamingCSVWriter(args.output_csv)
        try:
            async with TikTokScraper(args.config) as scraper:
                print(f"📍 {scraper.proxy_pool.get_pool_status()}")
                print()
                
                # Pass CSV writer callback for real-time result writing
                results = await scraper.scrape_batch(
                    video_ids,
                    on_result_callback=csv_writer.write_result
                )
        finally:
            # Always close CSV writer, even if scraper fails
            csv_writer.close()
        
        print()
        
        # Print summary
        await get_results_summary(args.output_csv)
        
        print()
        print("✓ Done!")
    
    except Exception as e:
        print(f"❌ Error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
