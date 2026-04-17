# Changes Summary

## Overview
Modified `main.py` to implement two key features:
1. **Terminal Log Output**: All terminal logs are now written to `terminal_log.txt` in the output folder
2. **Improved Re-scraping Logic**: Enhanced logic for checking existing post_IDs with conditional re-scraping

---

## Detailed Changes

### 1. Added Logging to File

**What was changed:**
- Added `import logging` to handle file-based logging
- Created `setup_logging(output_dir)` function that:
  - Sets up logging to write to both console and file
  - Creates `terminal_log.txt` in the output directory
  - Uses simple formatters to preserve original output appearance
- Created `log_message(logger, message)` helper function
- Replaced all `print()` statements with `log_message()` calls

**Benefits:**
- All terminal output is automatically saved to a file
- Useful for debugging and progress tracking
- No change to console output (still shows in terminal)

---

### 2. Enhanced Re-scraping Logic

**New function: `get_scraping_candidates()`**

This function implements the new logic:
```
- New videos (not in output CSV): Always scrape
- Existing videos (in output CSV): Only re-scrape if BOTH conditions are met:
  * raw_json column is not empty
  * downloaded column is False
- All other existing videos are skipped
```

**What it returns:**
- `videos_to_process`: List of video IDs to scrape (combines new + re-scrape candidates)
- `posts_to_update`: Set of post_IDs that are being re-scraped (for row updates)

**Example logic:**

```
DataFrame with existing posts:
post_id   raw_json            downloaded   Action
--------  ------------------  -----------  ------------------
123       {"data": "..."}     False        ✓ Re-scrape (both conditions met)
456       {"data": "..."}     True         ✗ Skip (already downloaded)
789       ""                  False        ✗ Skip (no raw_json)
999       NULL                False        ✗ Skip (no raw_json)
```

---

### 3. Updated CSV Writer

**Modified `StreamingCSVWriter` class:**

- Now accepts `posts_to_update` parameter (set of post_IDs being re-scraped)
- Handles row updates properly:
  - Loads existing CSV data when file exists
  - Keeps all rows EXCEPT those being re-scraped
  - On first write, outputs all preserved rows + new results
  - This ensures old rows for re-scraped posts are replaced with fresh data

**Key behavior:**
```
Original CSV:
post_id  description  raw_json         downloaded
-------  -----------  ---------------  -----------
123      "Old desc"   "{...json...}"   False
456      "Other"      "{...json...}"   True

If re-scraping post 123:
New CSV (after re-scrape):
post_id  description  raw_json         downloaded
-------  -----------  ---------------  -----------
123      "New desc"   "{...json...}"   True   (Updated!)
456      "Other"      "{...json...}"   True
```

---

### 4. Updated Main Execution Flow

**Changes to main() function:**

1. **Initialize logging early:**
   ```python
   logger = setup_logging(output_dir)
   log_message(logger, "Starting...")
   ```

2. **Use new scraping logic:**
   ```python
   videos_to_process, posts_to_update = await get_scraping_candidates(
       csv_output_path, video_ids, logger
   )
   ```

3. **Pass update info to CSV writer:**
   ```python
   csv_writer = StreamingCSVWriter(csv_output_path, posts_to_update=posts_to_update)
   ```

4. **All output to logging:**
   - Console still shows real-time output
   - Everything also saved to `terminal_log.txt`

---

## Output Files

### New Output
- **`terminal_log.txt`**: Text file in output directory containing all terminal logs

### Existing Outputs (Unchanged)
- **`metadata_output.csv`**: CSV file with metadata (now with potential row updates)
- **Videos/Images**: Downloaded media files

---

## Usage

Run the script as before:
```bash
python main.py input.csv --output-dir ../output
```

The script will:
1. Print output to console (as before)
2. Save all output to `../output/terminal_log.txt` (new)
3. Check existing posts and re-scrape only those with both:
   - `raw_json` not empty
   - `downloaded` = False
4. Update rows for re-scraped posts (new behavior)
5. Skip all other existing posts

---

## Testing Recommendations

1. **Test new scraping:** Run with a fresh output directory (empty CSV)
   - Should start from scratch
   
2. **Test partial success → retry:** 
   - Create CSV with post_id that has raw_json but downloaded=False
   - Re-run script
   - Should re-scrape and update that row

3. **Test skip logic:**
   - Create CSV with post_id that has raw_json but downloaded=True
   - Re-run script
   - Should skip that post

4. **Check terminal log:**
   - Verify `terminal_log.txt` contains all output
   - Check timestamps and progress messages

---

## Files Modified

- **`main.py`**: All changes described above
  - Added imports: `logging`, `Tuple`, `Set`
  - Added functions: `setup_logging()`, `log_message()`, `get_scraping_candidates()`
  - Modified: `StreamingCSVWriter`, `read_input_csv()`, `get_results_summary()`, `main()`
  - Removed: Old `load_already_scraped()` logic and replaced with new approach
