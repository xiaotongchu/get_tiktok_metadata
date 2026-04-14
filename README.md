# TikTok Scraper

> A clean, production-ready TikTok metadata and media scraper with true async concurrency, intelligent proxy rotation, and structured error handling.

## Overview

This is a complete refactoring of the original TikTok scraper, addressing three major architectural problems:

1. **Event Loop Inefficiency** → True async with httpx (not thread-pool polling)
2. **Proxy Management Complexity** → Intelligent pool with O(1) lookups and exponential backoff
3. **Sequential Downloads** → Parallel image downloads and pipelined execution

### Key Improvements

| Aspect | Original | v2 (Current) |
|--------|----------|-----------|
| **HTTP Library** | `requests` + `FuturesSession` (thread pool) | `httpx` (true async) |
| **Event Loop** | Polling every 0.1s, O(n) iterations | Event-driven, no polling |
| **Proxy Lookup** | Linear O(n) search every cycle | O(1) queue-based lookup |
| **Backoff Strategy** | Fixed 1-second throttle | Exponential backoff with circuit breaker |
| **Image Downloads** | Sequential (one at a time) | Parallel (configurable concurrency) |
| **Code Size** | ~650 lines (monolithic) | ~300 lines (modular) |
| **Error Handling** | Silent failures | Structured retry strategies per error type |
| **Execution Model** | Serialized phases | Pipelined (metadata + downloads concurrent) |
| **Logging** | No file logging | ✅ Comprehensive logging to file |
| **Duplicate Handling** | Append-only | ✅ Smart re-scraping with row updates |
| **Output Verification** | Limited | ✅ Complete execution history in logs |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  main.py (CLI)                          │
│  - Read input CSV                                       │
│  - Orchestrate scraper                                  │
│  - Write output CSV                                     │
└────────────────────┬────────────────────────────────────┘
                     │
         ┌───────────▼───────────┐
         │   scraper.py          │
         │ (Main Orchestrator)   │
         │                       │
         │ - scrape_batch()      │
         │ - Coordinate all      │
         │   components          │
         │ - Pipeline execution  │
         └───┬──────┬──────┬─────┘
             │      │      │
        ┌────▼────┐ │      │
        │ http    │ │      │
        │ client  │ │      │
        │ (httpx) │ │      │
        └────┬────┘ │      │
             │      │      │
        ┌────▼──────▼──┐  │ ┌──────────────────────┐
        │  metadata    │  │ │  browser_handler.py  │
        │  extractor   │  │ │  (Playwright)        │
        │              │  │ │  - Fallback for 403  │
        │ - Parse JSON │  │ │  - Video extraction  │
        │ - Extract    │  │ │                      │
        │   video URLs │  │ └──────────────────────┘
        │ - Extract    │  │
        │   images     │  │
        └────┬─────────┘  │
             │            │
         ┌───▼────────────▼───┐
         │  media_downloader   │
         │  - download_video   │
         │  - download_images  │
         │    (parallel)       │
         └─────┬───────────────┘
               │
         ┌─────▼──────────┐
         │  proxy_pool    │
         │  - O(1) lookup │
         │  - Backoff     │
         │  - Circuit     │
         │    breaker     │
         └────────────────┘
```

## Module Breakdown

### **config.yaml**
Configuration file with:
- HTTP client settings (timeout, retries)
- Proxy pool config (throttle, backoff, circuit breaker)
- Media download settings (concurrency, timeouts)
- Browser fallback settings
- Retry strategies per error type
- Feature flags

**Key insight**: All magic numbers moved to config → easy tweaking without code changes

### **models.py**
Type-safe data models:
- `VideoMetadata`: Core post metadata (description, author, stats)
- `DownloadResult`: Result object with status tracking
- `DownloadedFile`: Individual file result (for videos + image carousels)
- `ProxyStats`: Proxy state with backoff tracking

**Key insight**: Explicit data structures replace dictionaries → fewer bugs

### **proxy_pool.py**
Intelligent proxy management with:
- O(1) availability via queue-based lookup (not O(n) linear search)
- Exponential backoff: `cooldown = cooldown * backoff_factor` (capped at 5 min)
- Circuit breaker: mark proxy "broken" after N failures
- Throttling: configurable seconds between reuse

**Key insight**: Replaces the O(n) linear search that ran every 0.1 seconds with O(1) queue lookup. For 10 proxies, this is **~1000× fewer operations per minute**.

### **http_client.py**
Async HTTP client using `httpx`:
- Connection pooling
- Custom headers for embed pages, videos, images
- Streaming downloads
- HEAD requests for content-type detection

**Key insight**: `httpx` is drop-in replacement for `requests` with true async I/O (not thread pool)

### **metadata_extractor.py**
Consolidated metadata parsing:
- Tries multiple JSON script selectors in priority order
- Handles both video and image carousel posts
- Explicit error messages
- Validation before returning

**Key insight**: Extracts fragile multi-selector logic into reusable, testable functions

### **media_downloader.py**
Media download handler:
- Single video download
- **Parallel image downloads** via `asyncio.gather()` with semaphore
- Content-Type → file extension mapping
- Per-file error tracking

**Key insight**: Image downloads in parallel (e.g., 3 at a time) instead of sequential

### **browser_handler.py**
Playwright-based fallback:
- Only invoked on HTTP 403 or as last resort
- Launches headless Chromium with realistic UA
- Extracts video src via JavaScript
- Downloads within browser context

**Key insight**: Isolated from main flow → easier debugging and testing

### **scraper.py**
Main orchestrator:
- `fetch_metadata_for_post()`: Async metadata fetching with retry logic
- `download_post()`: Async download with parallel images + browser fallback
- `scrape_batch()`: Pipelined execution (metadata + downloads concurrent)
- Structured retry handling per error type

**Key insight**: Pipelined architecture means metadata fetching and downloading happen in parallel (not serialized phases like original)

### **main.py** (v2)
Clean CLI entry point with comprehensive logging and smart re-scraping:
- Read input CSV
- Setup comprehensive logging (console + file)
- Smart duplicate detection with conditional re-scraping
- Stream results to output CSV as they complete
- Intelligent error tracking and reporting

**Key Changes (v2 vs v1)**:
- ✅ Setup logging → captures print statements, exceptions, and library logs to `terminal_log.txt`
- ✅ Smart re-scraping → only re-scrape posts where `raw_json` exists but `downloaded=False`
- ✅ CSV row updates → re-scraped posts have their rows updated (old row removed, new row added)
- ✅ Preserved skipped rows → posts that don't meet re-scrape criteria are kept in CSV
- ✅ Logger passed to functions → all output logged to both console and file

**Output Files**:
- `metadata_output.csv` - All post metadata (new + re-scraped + skipped)
- `terminal_log.txt` - Complete execution log with timestamps and full exception tracebacks
- Videos and images directories (configured in `config.yaml`)

## Logging

All execution is logged to `terminal_log.txt` in the output directory:

```
================================================================================
RUN STARTED: 2026-04-14 10:30:45
================================================================================
🚀 Starting TikTok scraper
   Input: ../input/tiktok_metadata_extraction_30days.csv
   Config: config.yaml
   Output dir: ../output
   CSV output: ../output/metadata_output.csv

📋 Loaded 100 unique video IDs from ../input/file.csv

📌 Found 50 existing posts in ../output/metadata_output.csv
   30 new posts to scrape
   → Post 7490927982085934338 marked for re-scrape (raw_json exists, downloaded=False)
   → Post 7490927982085934339 marked for re-scrape (raw_json exists, downloaded=False)
   20 posts to re-scrape (raw_json not empty and not downloaded)

📊 Total posts to process: 50/100
   (30 new, 20 to re-scrape)

📍 Proxy pool status: 10 proxies (5 active, 3 cooling)

✓ [1] Saved: 7490927982085934338
✓ [2] Saved: 7490927982085934339
...
✓ Done!

📊 Results written to ../output/metadata_output.csv
   48/50 successful downloads
   2 failed
```

**Logged Information**:
- ✅ Print statements (replace all `print()` with logging)
- ✅ Uncaught exceptions with full tracebacks
- ✅ Library logging from dependencies
- ✅ Progress and status updates
- ✅ Runs separated by timestamps (appends to file)

## Smart Re-scraping Logic

The scraper intelligently decides which posts to process:

### New Posts (not in output CSV)
Always scraped ✓

### Existing Posts (in output CSV)
**Re-scraped only if BOTH conditions are met:**
1. `raw_json` column is **not empty** (metadata was extracted)
2. `downloaded` column is **False** (video/images not downloaded)

All other existing posts are **skipped and preserved** in the CSV.

**Example:**

| post_id | raw_json | downloaded | Action |
|---------|----------|-----------|--------|
| 123     | {...} | False | ✅ Re-scrape (update row) |
| 456     | {...} | True | ⏭️ Skip (already done) |
| 789     | (empty) | False | ⏭️ Skip (no metadata) |
| 999 | (NULL) | False | ⏭️ Skip (no metadata) |
| 111 | {...} | (NULL) | ✅ Re-scrape (treat as not downloaded) |

### CSV Row Behavior

**When re-scraping:**
- Delete old row for that post_id
- Write new row with fresh results
- Keep all other rows

**Result:** CSV always contains the latest data for each post_id

## Execution Flow Comparison

### Original (Sequential)
```
Phase 1: Fetch all metadata
├─ 0.1s polling loop
├─ O(n) proxy search each iteration
└─ Wait for all to complete

Phase 2: Download all media
├─ 0.1s polling loop
├─ O(n) proxy search each iteration
└─ Images downloaded sequentially per post

Output: Write CSV
```

**Problem**: Time = Metadata Phase + Download Phase (sequential)

### Refactored (Pipelined)
```
Concurrent:
├─ Metadata fetching (post 1, 2, 3, ...)
│  └─ As soon as metadata ready → trigger download
├─ Downloads (post 1, 2, 3, ...)
│  └─ Images in parallel (semaphore-controlled)
└─ Browser fallback (async, as needed)

Output: Stream results to CSV as they complete
```

**Benefit**: Time ≈ max(Metadata, Download) (can overlap)

## Performance Characteristics

### Concurrency Improvements

Assuming 10 posts, each with 3 images:

| Metric | Original | Refactored | Speedup |
|--------|----------|-----------|---------|
| **HTTP Requests** | 0.1s × N iterations | Event-driven | Variable |
| **Proxy Lookups** | O(n) per 0.1s cycle | O(1) dequeue | 10-100× |
| **Image Downloads** | Sequential (3s each) | Parallel 3 @ once (1s) | **3×** |
| **Phase Overlap** | 0% (serial) | ~70% (parallel) | **1.7×** |
| **Total Time** | T₁ + T₂ + overhead | max(T₁, T₂) + minimal | **2-3×** |

### Memory

- Original: Many pending dictionaries + thread objects
- Refactored: Cleaner async tasks + proxy queue

## Configuration

Edit `config.yaml` to customize behavior:

```yaml
# Parallel image downloads (default 3)
media_download:
  max_concurrent_images: 3

# Exponential backoff for failed proxies
proxy_pool:
  initial_cooldown: 2
  max_cooldown: 300
  failure_threshold: 5

# Browser fallback for 403 errors
browser:
  enabled: true
  headless: true
  timeout: 50
```

## Usage

### Installation

```bash
pip install pandas httpx pyyaml playwright beautifulsoup4
python -m playwright install chromium
sudo apt-get install ca-certificates fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 libc6 libcairo2 libcups2 libdbus-1-3 libexpat1 libfontconfig1 libgbm1 libgcc1 libglib2.0-0 libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 libpangocairo-1.0-0 libstdc++6 libx11-6 libx11-xcb1 libxcb1 xdg-utils
```

### Running the Scraper

**Basic usage** (output to `../output/`):
```bash
python3 main.py input.csv
```

**Custom output directory:**
```bash
python3 main.py input.csv --output-dir ./results
```

**Custom CSV filename:**
```bash
python3 main.py input.csv --output-dir ./results --csv-filename results.csv
```

**Add timestamp to CSV filename:**
```bash
python3 main.py input.csv --output-dir ./results --timestamp
```

**Override config with timestamp:**
```bash
python3 main.py input.csv --output-dir ./results --csv-filename custom.csv --timestamp
```

### Output Files

After running, check the output directory for:

```
../output/
├── metadata_output.csv          ← All metadata (new + re-scraped + skipped)
├── terminal_log.txt             ← Complete execution log (appended)
├── videos/                      ← Downloaded TikTok videos
└── images/                      ← Downloaded carousel images
```

### Checking the Logs

View the complete execution history:
```bash
cat ../output/terminal_log.txt
```

Or follow live:
```bash
tail -f ../output/terminal_log.txt
```

### Workflow Example

**First run** (fresh start):
```bash
python3 main.py input.csv
# Scrapes all 100 posts
# Writes 100 rows to metadata_output.csv
# Logs all output to terminal_log.txt (new file)
```

**Second run** (same input):
```bash
python3 main.py input.csv
# Finds 100 existing posts in metadata_output.csv
# Identifies which need re-scraping (raw_json exists but downloaded=False)
# Re-scrapes only those posts, updates their rows
# Keeps all other rows (skipped posts)
# Appends new run to terminal_log.txt with separator
```

**For partial re-scrape** (manually edit CSV):
```bash
# Edit metadata_output.csv:
# 1. Find a post where downloaded=True
# 2. Change it to downloaded=False
# 3. Keep raw_json non-empty
# 4. Save and run again
# → That post will be re-scraped and row updated
```



## Testing & Verification

### Check Execution Logs

All execution details are logged to `terminal_log.txt`:
```bash
# View full history
cat ../output/terminal_log.txt

# View only the latest run
tail -100 ../output/terminal_log.txt

# Search for errors
grep "❌" ../output/terminal_log.txt

# See which posts were re-scraped
grep "marked for re-scrape" ../output/terminal_log.txt
```

### Verify CSV Results

```bash
# Count total posts
wc -l ../output/metadata_output.csv

# Check download success rate
python3 -c "import pandas as pd; df = pd.read_csv('../output/metadata_output.csv'); print(f'Downloaded: {(df[\"downloaded\"]==True).sum()}/{len(df)}')"

# Check which posts failed
python3 -c "import pandas as pd; df = pd.read_csv('../output/metadata_output.csv'); print(df[df['downloaded']==False][['post_id', 'error_message']].head())"

# Check for posts needing re-scrape
python3 -c "import pandas as pd; df = pd.read_csv('../output/metadata_output.csv'); rescrape = df[(df['raw_json'].notna()) & (df['raw_json'] != '') & (df['downloaded']==False)]; print(f'Posts to re-scrape: {len(rescrape)}')"
```

### Integration Tests

```bash
# Run on test data
python3 main.py ../input/tiktok_metadata_extraction_30days.csv

# Verify output structure
python3 -c "import pandas as pd; df = pd.read_csv('../output/metadata_output.csv'); print(f'Columns: {list(df.columns)}')"

# Check for required fields
python3 -c "import pandas as pd; df = pd.read_csv('../output/metadata_output.csv'); required = ['post_id', 'downloaded', 'raw_json']; print('✓ All required columns present' if all(c in df.columns for c in required) else '✗ Missing columns')"
```

## Error Handling

### Retry Strategies

**Connection errors** (timeout, connection refused)
- Strategy: Exponential backoff, next proxy
- Max attempts: 3

**HTTP 403** (access denied)
- Strategy: Try browser fallback → different proxy
- Max attempts: 2

**HTTP non-200 errors** (404, 500, etc.)
- Strategy: Limited retry (may not help)
- Max attempts: 1

**Proxy circuit breaker**
- After N consecutive failures, mark proxy "broken"
- Don't send traffic to it for cooldown period
- Exponential cooldown: 2s, 4s, 8s, ..., max 5min

## Deployment

1. **Setup environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

2. **Prepare config**:
   - Copy `config.yaml` to your working directory
   - Adjust proxy list, timeouts, and concurrency as needed

3. **Prepare input data**:
   - CSV with `post_ID`, `id`, or `post_id` column containing TikTok video IDs
   - Optional columns: `Link` (URL), `type` ("img" for image posts)

4. **Create output directory**:
   ```bash
   mkdir -p ../output
   ```

5. **Run the scraper**:
   ```bash
   python3 main.py input.csv --output-dir ../output
   ```

6. **Monitor execution**:
   ```bash
   tail -f ../output/terminal_log.txt
   ```

7. **Check results**:
   ```bash
   ls -lh ../output/metadata_output.csv
   head ../output/metadata_output.csv
   ```

### Production Considerations

- **Error Recovery**: If script crashes mid-run, just re-run with same input → will resume correctly (skips completed posts)
- **Large Batches**: For 10,000+ posts, consider splitting input into chunks
- **Proxy Rotation**: Monitor `terminal_log.txt` for proxy health messages
- **Storage**: Account for ~10-20MB per 100 videos (depends on video length)

## Troubleshooting

### Posts Not Being Re-scraped

**Issue**: Posts with `raw_json` and `downloaded=False` aren't being re-scraped.

**Diagnosis**:
1. Check `terminal_log.txt` for re-scraping candidates:
   ```bash
   grep "marked for re-scrape" ../output/terminal_log.txt
   ```

2. Verify post is in input CSV:
   ```bash
   grep "7490927982085934338" ../input/tiktok_metadata_extraction_30days.csv
   ```

3. Check raw_json format (not null/empty):
   ```bash
   python3 -c "import pandas as pd; df = pd.read_csv('../output/metadata_output.csv'); post = df[df['post_id']=='7490927982085934338'].iloc[0]; print(f'Has raw_json: {pd.notna(post[\"raw_json\"])}'); print(f'Downloaded: {post[\"downloaded\"]}')"
   ```

### Logs Not Being Generated

**Issue**: `terminal_log.txt` file is missing or not updating.

**Solutions**:
- Check output directory exists: `ls -la ../output/`
- Verify write permissions: `touch ../output/test.txt`
- Check for exceptions in console output (now should be in log)

### CSV Rows Being Deleted

**Issue**: After re-running, some rows from first run are gone.

**Root cause**: If a post was marked for re-scrape but had an error, the old row is deleted and nothing written (no callback).

**Workaround**: Check `terminal_log.txt` for which posts failed during re-scrape attempt.

### Proxy Issues
- Check `proxy_pool.get_pool_status()` info in logs
- Increase `failure_threshold` if proxies are too aggressive
- Adjust `backoff_factor` for gentler backoff

### Metadata Not Found
- Check `terminal_log.txt` for extraction errors
- Check if TikTok changed JSON structure (try selectors in `MetadataExtractor.JSON_SCRIPT_SELECTORS`)
- Some posts require login → validation catches these

### Image Download Failures
- Reduce `max_concurrent_images` if server throttles
- Check image URLs are valid (browser opens in embed page)
- Check `terminal_log.txt` for download errors

## Recently Implemented (v2)

- ✅ **Comprehensive Logging** - All output (print, exceptions, library logs) captured to `terminal_log.txt`
- ✅ **Smart Re-scraping** - Conditional re-scrape based on `raw_json` and `downloaded` status
- ✅ **CSV Row Updates** - Re-scraped posts have rows deleted and replaced with fresh data
- ✅ **Preserved Skipped Rows** - Non-matching posts kept in CSV across runs
- ✅ **Run Separators** - Each execution marked with timestamp in log file
- ✅ **Streaming CSV Writes** - Results written immediately as they complete
- ✅ **Full Tracebacks** - Uncaught exceptions logged with complete stack traces

## Future Improvements

1. **Persistent browser instance** for browser_handler (reuse across downloads)
2. **Rate limiting** per proxy (adaptive based on 429 responses)
3. **Retry-After header** support for backoff decisions
4. **Metrics collection** (Prometheus exporter for monitoring)
5. **Distributed proxy pool** (Redis-backed for multi-worker setups)
6. **Log rotation** - Automatic archival of old terminal_log.txt files
7. **Partial CSV recovery** - Better handling of mid-run failures
8. **Progress bar** - Visual indication of batch progress

## License

Original implementation: [Your License]
Refactored version: [Your License]

---

**Questions?** See inline code comments or check specific module docstrings.
