"""
Standalone script to get TikTok metadata and download videos from post IDs in a CSV file.

This version uses Playwright to launch a headless Chromium browser when a
video download URL returns a 403 error. The browser loads the embed page and
extracts the <video> element's src attribute after the page's JavaScript has
populated it.

Setup steps on a server:

1. Create/activate a Python virtual environment.
2. Install dependencies:
       pip install pandas requests-futures beautifulsoup4 playwright
3. Download the browser binaries for Playwright:
       python -m playwright install chromium

The resulting script can be bundled with your project by including the
playwright dependency and ensuring the install command is run during
deployment. Running Playwright in headless mode works on typical Linux/Mac
servers; make sure necessary libraries (e.g. libX11) are present if using
Linux.
"""
import requests
import asyncio
import time
import json
import csv
import os
import re
from pathlib import Path

from requests_futures.sessions import FuturesSession
from bs4 import BeautifulSoup
import pandas as pd
from playwright.async_api import async_playwright


class TikTokScraper:
    proxy_map = None
    proxy_sleep = 1
    headers = {
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
    last_proxy_update = 0
    last_time_proxy_available = None
    no_available_proxy_timeout = 600
    consecutive_failures = 0

    VIDEO_NOT_FOUND = "oh no, sire, no video was found"

    def __init__(self, proxies=None):
        self.proxy_map = {}
        self.proxies = proxies or ["__localhost__"]

    def update_proxies(self):
        all_proxies = self.proxies
        if not all_proxies:
            all_proxies = ["__localhost__"]

        for proxy in all_proxies:
            if proxy in self.proxy_map:
                continue
            else:
                self.proxy_map[proxy] = {
                    "busy": False,
                    "url": None,
                    "next_request": 0
                }

        for proxy in list(self.proxy_map.keys()):
            if proxy not in all_proxies:
                del self.proxy_map[proxy]

    def get_available_proxies(self):
        if self.last_proxy_update < time.time():
            self.update_proxies()
            self.last_proxy_update = time.time() + 5

        available_proxies = [proxy for proxy in self.proxy_map if
                             not self.proxy_map[proxy]["busy"] and self.proxy_map[proxy]["next_request"] <= time.time()]

        if not available_proxies:
            if self.last_time_proxy_available is None:
                print("No available proxy found at start of request_metadata")
                self.last_time_proxy_available = time.time()

            if self.last_time_proxy_available + self.no_available_proxy_timeout < time.time():
                raise Exception(f"Error: No proxy found available after {self.no_available_proxy_timeout}")
        else:
            self.last_time_proxy_available = time.time()

        return available_proxies

    async def fetch_video_src_with_browser(self, embed_url):
        """Use a headless browser to load the embed page, fetch the video src, and download it.

        The page makes an XHR/Fetch request to an API endpoint which contains the
        actual video URL.  We install a response handler to inspect JSON bodies
        for an MP4 URL, and also trigger playback to force the request.
        
        Returns a tuple of (url, data) where data is the downloaded video bytes.
        """
        src_holder = {"url": None}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            # Create context with realistic browser properties
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Mobile Safari/537.36",
                viewport={"width": 390, "height": 844},
                device_scale_factor=2,
                locale="en-NL",
            )
            page = await context.new_page()

            # First navigate to tiktok.com to establish session and get cookies
            try:
                await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
            except Exception as e:
                pass

            # async def on_request(request):
            #     try:
            #         url = request.url
            #         if url.endswith('.mp4') or 'tiktokcdn' in url or 'v16-webapp-prime' in url or 'v19-webapp-prime' in url or 'v32-webapp-prime' in url:
            #             src_holder['url'] = url
            #     except Exception:
            #         pass

            # async def on_response(response):
            #     try:
            #         ct = response.headers.get("content-type", "")
            #         if "application/json" in ct:
            #             text = await response.text()
            #             # look for an mp4 URL inside the JSON string
            #             m = re.search(r'https?://[^\"]+\.mp4', text)
            #             if m:
            #                 src_holder["url"] = m.group(0)
            #     except Exception:
            #         pass

            # page.on("request", on_request)
            # page.on("response", on_response)

            # Now navigate to embed page
            try:
                await page.goto(embed_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                pass

            # ensure video element is on the page
            try:
                await page.wait_for_selector("video", timeout=10000)
            except Exception:
                pass

            # trigger playback, which often causes the API call
            await page.evaluate(
                "() => { const v = document.querySelector('video'); if (v) { v.play().catch(()=>{}); } }"
            )
            # keep trying to wait for the requests and page javascript to populate the new src
            # fetch video.src and only contine when changed.
            now = time.time()
            try:
                video_src = await page.evaluate("document.querySelector('video').src")
                if video_src:
                    src_holder["url"] = video_src
                while True:
                    new_video_src = await page.evaluate("document.querySelector('video').src")
                    if new_video_src != video_src:
                        video_src = new_video_src
                        break
                    await page.wait_for_timeout(50)
                    if time.time() - now > 15000:
                        print("Timed out waiting for video src to update in browser, proceeding with best effort src if available")
                        break
            except Exception:
                pass

            # Download the video in the same browser context before closing
            video_data = None
            if src_holder.get("url"):
                try:
                    # Set headers matching the working request as closely as possible
                    extra_headers = {
                        "accept": "*/*",
                        "accept-language": "nl,en-US;q=0.9,en;q=0.8",
                        "accept-encoding": "identity;q=1, *;q=0",
                        "priority": "i",
                        "range": "bytes=0-",
                        "sec-ch-ua": "\"Not(A:Brand\";v=\"8\", \"Chromium\";v=\"144\", \"Google Chrome\";v=\"144\"",
                        "sec-ch-ua-mobile": "?1",
                        "sec-ch-ua-platform": "\"Android\"",
                        "sec-fetch-dest": "video",
                        "sec-fetch-mode": "no-cors",
                        "sec-fetch-site": "same-site",
                        "referer": "https://www.tiktok.com/",
                    }
                    resp = await page.request.get(src_holder["url"], headers=extra_headers)
                    # Accept both 200 (OK) and 206 (Partial Content - from range requests)
                    if resp.status in [200, 206]:
                        video_data = await resp.body()
                except Exception as e:
                    pass

            await browser.close()

        return src_holder.get("url"), video_data

    async def download_bytes_with_browser(self, url):
        """Fetch raw bytes for a URL using Playwright's request API."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            resp = await page.request.get(url)
            status = resp.status
            if status == 200:
                data = await resp.body()
                await browser.close()
                return data, None
            else:
                await browser.close()
                return None, status

    def release_proxy(self, url):
        used_proxy = [proxy for proxy in self.proxy_map if self.proxy_map[proxy]["url"] == url]
        if used_proxy:
            used_proxy = used_proxy[0]
            self.proxy_map[used_proxy].update({
                "busy": False,
                "next_request": time.time() + self.proxy_sleep
            })
        else:
            print(f"Unable to find and release proxy associated with {url}")

    async def request_metadata(self, urls):
        session = FuturesSession()
        session.headers.update(self.headers)
        tiktok_requests = {}
        finished = 0
        num_urls = len(urls)
        seen_urls = set()

        results = []
        failed = 0
        dupes = 0
        retries = {}

        while urls or tiktok_requests:
            await asyncio.sleep(0.1)

            available_proxies = self.get_available_proxies()

            for available_proxy in available_proxies:
                url = None
                while urls and url is None:
                    url = urls.pop(0)
                    url = url.replace("https://", "http://")

                    if url in seen_urls and url not in retries:
                        finished += 1
                        dupes += 1
                        print("Skipping duplicate of %s" % url)
                        url = None
                        continue

                    print(f"Requesting: {url}")
                    proxy = {"http": available_proxy,
                             "https": available_proxy} if available_proxy != "__localhost__" else None
                    tiktok_requests[url] = session.get(url, proxies=proxy, timeout=30)
                    seen_urls.add(url)
                    self.proxy_map[available_proxy].update({
                        "busy": True,
                        "url": url
                    })

            for url in list(tiktok_requests.keys()):
                request = tiktok_requests[url]
                if not request.done():
                    continue

                finished += 1
                self.release_proxy(url)

                exception = request.exception()
                if exception:
                    failed += 1
                    if isinstance(exception, requests.exceptions.RequestException):
                        print("Video at %s could not be retrieved (%s: %s)" % (url, type(exception).__name__, exception))
                    else:
                        raise exception

                try:
                    response = request.result()
                except requests.exceptions.RequestException:
                    if url not in retries or retries[url] < 3:
                        if url not in retries:
                            retries[url] = 0
                        retries[url] += 1
                        urls.append(url)
                    continue
                finally:
                    del tiktok_requests[url]

                if response.status_code == 404:
                    failed += 1
                    print("Video at %s no longer exists (404), skipping" % response.url)
                    continue

                elif response.status_code != 200:
                    failed += 1
                    print("Received unexpected HTTP response %i for %s, skipping." % (response.status_code, response.url))
                    continue

                soup = BeautifulSoup(response.text, "html.parser")
                sigil = soup.select_one("script#SIGI_STATE")

                if not sigil:
                    sigil = soup.select_one("script#__UNIVERSAL_DATA_FOR_REHYDRATION__")

                if not sigil:
                    if url not in retries or retries[url] < 3:
                        if url not in retries:
                            retries[url] = 0
                        retries[url] += 1
                        urls.append(url)
                        print("No embedded metadata found for video %s, retrying" % url)
                    else:
                        failed += 1
                        print("No embedded metadata found for video %s, skipping" % url)
                    continue

                try:
                    if sigil.text:
                        metadata = json.loads(sigil.text)
                    elif sigil.contents and len(sigil.contents) > 0:
                        metadata = json.loads(sigil.contents[0])
                    else:
                        failed += 1
                        print("Embedded metadata was found for video %s, but it could not be parsed, skipping" % url)
                        continue
                except json.JSONDecodeError:
                    failed += 1
                    print("Embedded metadata was found for video %s, but it could not be parsed, skipping" % url)
                    continue

                for video in self.reformat_metadata(metadata):
                    if video == self.VIDEO_NOT_FOUND:
                        failed += 1
                        print(f"Video for {url} not found, may have been removed, skipping")
                        continue

                    if not video.get("stats") or video.get("createTime") == "0":
                        print(f"Empty metadata returned for video {url} ({video['id']}), skipping. This likely means that the post requires logging in to view.")
                        continue
                    else:
                        results.append(video)

                    print("Processed %s of %s TikTok URLs" % ("{:,}".format(finished), "{:,}".format(num_urls)))

        notes = []
        if failed:
            notes.append("%s URL(s) failed or did not exist anymore" % "{:,}".format(failed))
        if dupes:
            notes.append("skipped %s duplicate(s)" % "{:,}".format(dupes))

        if notes:
            print("Dataset completed, but not all URLs were collected (%s)." % ", ".join(notes))

        return results

    def reformat_metadata(self, metadata):
        if "__DEFAULT_SCOPE__" in metadata and "webapp.video-detail" in metadata["__DEFAULT_SCOPE__"]:
            try:
                video = metadata["__DEFAULT_SCOPE__"]["webapp.video-detail"]["itemInfo"]["itemStruct"]
            except KeyError as e:
                if "statusCode" in metadata["__DEFAULT_SCOPE__"]["webapp.video-detail"]:
                    yield self.VIDEO_NOT_FOUND
                    return
                else:
                    raise e.__class__ from e

            metadata = {"ItemModule": {
                video["id"]: video
            }}

        if "ItemModule" in metadata:
            for video_id, item in metadata["ItemModule"].items():
                if "CommentItem" in metadata:
                    comments = {i: c for i, c in metadata["CommentItem"].items() if c["aweme_id"] == video_id}
                    if "UserModule" in metadata:
                        for comment_id in list(comments.keys()):
                            username = comments[comment_id]["user"]
                            comments[comment_id]["user"] = metadata["UserModule"].get("users", {}).get(username, username)
                else:
                    comments = {}

                yield {**item, "comments": list(comments.values())}

    async def download_videos(self, video_ids, staging_area, max_videos):
        video_download_headers = {
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
        image_download_headers = {
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
        session = FuturesSession()

        download_results = {}
        downloaded_videos = 0
        metadata_collected = 0
        video_requests = {}
        video_download_urls = []
        image_download_urls = []  # Queue for image downloads: (video_id, [(image_url, image_index), ...])

        soft_fail_limit = 5
        hard_fail_limit = 10
        soft_fail_pause_seconds = 30

        async def abort_inflight_and_raise(message: str):
            try:
                for reqinfo in video_requests.values():
                    try:
                        reqinfo["request"].cancel()
                    except Exception:
                        pass
                max_timeout = time.time() + 20
                while any(not r["request"].done() for r in video_requests.values()) and time.time() < max_timeout:
                    await asyncio.sleep(0.5)
            finally:
                raise Exception(message)

        while video_ids or video_download_urls or image_download_urls or video_requests:
            await asyncio.sleep(0.1)

            available_proxies = self.get_available_proxies()

            for available_proxy in available_proxies:
                if downloaded_videos >= max_videos:
                    video_ids = []
                    video_download_urls = []
                    # image_download_urls = []
                    break

                if image_download_urls:
                    video_id, image_urls_list = image_download_urls.pop(0)
                    # Process first image in the list
                    if image_urls_list:
                        image_url, image_index = image_urls_list.pop(0)
                        proxy = {"http": available_proxy,
                                 "https": available_proxy} if available_proxy != "__localhost__" else None
                        session.headers.update(image_download_headers)
                        video_requests[image_url] = {
                            "request": session.get(image_url, proxies=proxy, timeout=30),
                            "video_id": video_id,
                            "image_index": image_index,
                            "type": "image_download",
                        }
                        self.proxy_map[available_proxy].update({
                            "busy": True,
                            "url": image_url
                        })
                        # Put remaining images back on the queue if any
                        if image_urls_list:
                            image_download_urls.insert(0, (video_id, image_urls_list))
                elif video_download_urls:
                    video_id, video_download_url = video_download_urls.pop(0)
                    proxy = {"http": available_proxy,
                             "https": available_proxy} if available_proxy != "__localhost__" else None
                    session.headers.update(video_download_headers)
                    video_requests[video_download_url] = {
                        "request": session.get(video_download_url, proxies=proxy, timeout=30),
                        "video_id": video_id,
                        "type": "download",
                    }
                    self.proxy_map[available_proxy].update({
                        "busy": True,
                        "url": video_download_url
                    })
                elif video_ids:
                    video_id = video_ids.pop(0)
                    url = f"https://www.tiktok.com/embed/v2/{video_id}"

                    proxy = {"http": available_proxy,
                             "https": available_proxy} if available_proxy != "__localhost__" else None
                    session.headers.update(self.headers)
                    video_requests[url] = {
                        "request": session.get(url, proxies=proxy, timeout=30),
                        "video_id": video_id,
                        "type": "metadata",
                    }
                    self.proxy_map[available_proxy].update({
                        "busy": True,
                        "url": url
                    })

            pause_after_this_iteration = False

            for url in list(video_requests.keys()):
                video_id = video_requests[url]["video_id"]
                request = video_requests[url]["request"]
                request_type = video_requests[url]["type"]
                request_metadata = {
                    "success": False,
                    "url": url,
                    "error": None,
                    "post_ids": [video_id],
                }
                if not request.done():
                    continue

                self.release_proxy(url)

                try:
                    response = request.result()
                except requests.exceptions.RequestException as e:
                    error_message = f"URL {url} could not be retrieved ({type(e).__name__}: {e})"
                    request_metadata["error"] = error_message
                    download_results[video_id] = request_metadata
                    print(error_message)
                    self.consecutive_failures += 1
                    if self.consecutive_failures >= hard_fail_limit:
                        await abort_inflight_and_raise(f"Too many consecutive failures ({self.consecutive_failures}), stopping")
                    if self.consecutive_failures == soft_fail_limit:
                        pause_after_this_iteration = True
                    del video_requests[url]
                    continue

                # Special handling for download 403: try fallback from video src by using a headless browser
                if request_type == "download" and response.status_code == 403:
                    embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"
                    try:
                        video_src, video_data = await self.fetch_video_src_with_browser(embed_url)
                        if video_data:
                            # write file with downloaded data from browser context
                            with open(staging_area / f"{video_id}.mp4", "wb") as f:
                                f.write(video_data)
                            request_metadata["success"] = True
                            request_metadata["files"] = [{"filename": str(video_id) + ".mp4", "success": True}]
                            download_results[video_id] = request_metadata

                            downloaded_videos += 1
                            print("Downloaded %i/%i videos" % (downloaded_videos, max_videos))
                            # skip the rest of processing for this entry
                            continue
                        else:
                            error_message = "No video data obtained from headless browser fallback"
                            request_metadata["error"] = error_message
                            download_results[video_id] = request_metadata
                            print(error_message)
                            self.consecutive_failures += 1
                            continue
                    except Exception as e:
                        error_message = f"Fallback browser request failed: {e}"
                        request_metadata["error"] = error_message
                        download_results[video_id] = request_metadata
                        print(error_message)
                        self.consecutive_failures += 1
                        continue

                if response.status_code != 200:
                    error_message = f"Received unexpected HTTP response ({response.status_code}) {response.reason} for {url}, skipping."
                    request_metadata["error"] = error_message
                    download_results[video_id] = request_metadata
                    print(error_message)
                    self.consecutive_failures += 1
                    if self.consecutive_failures >= hard_fail_limit:
                        await abort_inflight_and_raise(f"Too many consecutive failures ({self.consecutive_failures}), stopping")
                    if self.consecutive_failures == soft_fail_limit:
                        pause_after_this_iteration = True
                    continue

                if request_type == "metadata":
                    soup = BeautifulSoup(response.text, "html.parser")
                    json_source = soup.select_one("script#__FRONTITY_CONNECT_STATE__")
                    video_metadata = None
                    try:
                        if json_source and json_source.text:
                            video_metadata = json.loads(json_source.text)
                        elif json_source and json_source.contents:
                            video_metadata = json.loads(json_source.contents[0])
                    except json.JSONDecodeError as e:
                        print(f"JSONDecodeError for video {video_id} metadata: {e}")

                    if not video_metadata:
                        error_message = f"Failed to find metadata for video {video_id}"
                        request_metadata["error"] = error_message
                        download_results[video_id] = request_metadata
                        print(error_message)
                        self.consecutive_failures += 1
                        if self.consecutive_failures >= hard_fail_limit:
                            await abort_inflight_and_raise(f"Too many consecutive failures ({self.consecutive_failures}), stopping")
                        if self.consecutive_failures == soft_fail_limit:
                            pause_after_this_iteration = True
                        continue

                    try:
                        video_data = list(video_metadata["source"]["data"].values())[0]["videoData"]
                        item_infos = video_data["itemInfos"]
                        
                        # Check if this is an image post
                        image_post_info = video_data.get("imagePostInfo", {})
                        display_images = image_post_info.get("displayImages", [])
                        
                        if display_images:
                            # This is an image post - extract URLs from urlList in each image object
                            image_urls = []
                            for idx, img_obj in enumerate(display_images):
                                url_list = img_obj.get("urlList", [])
                                if url_list:
                                    # Use the first URL in the list (usually the best quality)
                                    image_urls.append((url_list[0], idx))
                            
                            if image_urls:
                                self.consecutive_failures = 0
                                image_download_urls.append((video_id, image_urls))
                                metadata_collected += 1
                                print(f"Collected metadata for image post {video_id} with {len(image_urls)} images")
                            else:
                                error_message = f"vid: {video_id} - image post found but no valid URLs in urlList"
                                request_metadata["error"] = error_message
                                download_results[video_id] = request_metadata
                                print(error_message)
                                self.consecutive_failures += 1
                                if self.consecutive_failures >= hard_fail_limit:
                                    await abort_inflight_and_raise(f"Too many consecutive failures ({self.consecutive_failures}), stopping")
                                continue
                        else:
                            # This is a video post
                            url = item_infos["video"]["urls"][0]
                            self.consecutive_failures = 0
                            video_download_urls.append((video_id, url))
                            metadata_collected += 1
                            print("Collected metadata for %i/%i videos" % (metadata_collected, max_videos))
                    except (KeyError, IndexError):
                        error_message = f"vid: {video_id} - failed to find video or image download URL"
                        request_metadata["error"] = error_message
                        download_results[video_id] = request_metadata
                        print(error_message)
                        self.consecutive_failures += 1
                        if self.consecutive_failures >= hard_fail_limit:
                            await abort_inflight_and_raise(f"Too many consecutive failures ({self.consecutive_failures}), stopping")
                        continue

                elif request_type == "download":
                    with open(staging_area / f"{video_id}.mp4", "wb") as f:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                f.write(chunk)
                    request_metadata["success"] = True
                    request_metadata["files"] = [{"filename": str(video_id) + ".mp4", "success": True}]
                    download_results[video_id] = request_metadata

                    downloaded_videos += 1
                    print("Downloaded %i/%i videos" % (downloaded_videos, max_videos))

                elif request_type == "image_download":
                    image_index = video_requests[url].get("image_index", 0)
                    # Determine image extension from content-type or default to jpg
                    content_type = response.headers.get("content-type", "image/jpeg")
                    ext = "jpg"
                    if "png" in content_type:
                        ext = "png"
                    elif "webp" in content_type:
                        ext = "webp"
                    elif "gif" in content_type:
                        ext = "gif"
                    
                    image_filename = f"{video_id}_{image_index}.{ext}"
                    with open(staging_area / image_filename, "wb") as f:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                f.write(chunk)
                    
                    # Update or initialize result metadata for this video
                    if video_id not in download_results:
                        download_results[video_id] = {
                            "success": False,
                            "url": url,
                            "error": None,
                            "post_ids": [video_id],
                            "files": []
                        }
                    
                    download_results[video_id]["files"].append({
                        "filename": image_filename,
                        "success": True,
                        "image_index": image_index
                    })
                    
                    # Mark as success once we've downloaded at least one image
                    download_results[video_id]["success"] = True
                    
                    downloaded_videos += 1
                    print("Downloaded image %i for post %s (%i/%i total)" % (image_index, video_id, downloaded_videos, max_videos))

                # Delete the request from tracking after processing
                del video_requests[url]

            if pause_after_this_iteration:
                print(f"Encountered {soft_fail_limit} consecutive failures after previous successes; pausing {soft_fail_pause_seconds}s before retrying")
                await asyncio.sleep(soft_fail_pause_seconds)

        return download_results


async def main():
    # Read CSV
    df = pd.read_csv('tiktok_test_data_10.csv')
    unique_ids = [str(id) for id in df['post_ID'].unique()]
    urls = df['Link'].unique()

    scraper = TikTokScraper()

    # Get metadata
    metadata_list = await scraper.request_metadata(list(urls))
    metadata_dict = {m['id']: m for m in metadata_list}

    # Download videos
    staging_area = Path('videos')
    staging_area.mkdir(exist_ok=True)
    download_results = await scraper.download_videos(unique_ids.copy(), staging_area, len(unique_ids))

    # Prepare output
    output_rows = []
    for post_id in unique_ids:
        meta = metadata_dict.get(post_id, {})
        download_info = download_results.get(post_id, {})
        downloaded = download_info.get('success', False)
        error = download_info.get('error', '')

        row = {
            'post_id': post_id,
            'description': meta.get('desc', ''),
            'author_name': meta.get('authorMeta', {}).get('name', ''),
            'author_id': meta.get('authorMeta', {}).get('id', ''),
            'create_time': meta.get('createTime', ''),
            'views': meta.get('stats', {}).get('playCount', 0),
            'likes': meta.get('stats', {}).get('diggCount', 0),
            'shares': meta.get('stats', {}).get('shareCount', 0),
            'comments': meta.get('stats', {}).get('commentCount', 0),
            'downloaded': downloaded,
            'error_message': error
        }
        output_rows.append(row)

    # Write CSV
    with open('metadata_output.csv', 'w', newline='', encoding='utf-8') as f:
        if output_rows:
            writer = csv.DictWriter(f, fieldnames=output_rows[0].keys())
            writer.writeheader()
            writer.writerows(output_rows)

    print("Processing complete. Output saved to output.csv and videos to videos/ folder.")


if __name__ == "__main__":
    asyncio.run(main())