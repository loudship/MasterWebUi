"""
title: Gigascrape Deep-Web Agent
author: Antigravity
description: Extraction-centric tool bypassing mainstream SEO for deep-web discovery. Relies on internal SearXNG proxy and Crawl4AI.
version: 1.0.0
"""
import asyncio
import json
import logging
from pydantic import BaseModel, Field
import aiohttp

class Tools:
    class Valves(BaseModel):
        searxng_endpoint_url: str = Field(default="http://searxng:8080", description="Internal SearXNG endpoint")
        crawl4ai_endpoint_url: str = Field(default="http://crawl4ai-proxy:8000", description="Internal Crawl4AI proxy endpoint")
        max_search_depth: int = Field(default=3, description="Maximum number of URLs to crawl simultaneously")
        crawler_timeout_seconds: int = Field(default=60, description="Timeout per crawl operation")
        ha_token: str = Field(default="", description="Home Assistant API token for zero-trust compliance", json_schema_extra={"type": "password"})

    def __init__(self):
        self.valves = self.Valves()

    async def gigascrape_discover(self, search_intent: str, __event_emitter__=None) -> str:
        """
        Executes a deep-web discovery loop isolating non-mainstream engines to prevent context window poisoning, and retrieves raw structural DOM data via Crawl4AI proxy.
        :param search_intent: The refined semantic tokens or query to scrape from obscure internet archives.
        """
        if not self.valves.searxng_endpoint_url or not self.valves.crawl4ai_endpoint_url:
            return "Error: Required Valves (searxng_endpoint_url or crawl4ai_endpoint_url) are null or misconfigured."
        
        async def emit_status(description: str, done: bool = False, hidden: bool = False):
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {"description": description, "done": done, "hidden": hidden}
                })

        await emit_status("Initializing SearXNG...", done=False, hidden=False)
        
        # Mainstream evasion: keep_only target engines
        engines_list = "arch linux wiki,wikidata,marginalia,crossref,semantic scholar"
        
        discovered_urls = []
        try:
            async with aiohttp.ClientSession() as session:
                searxng_url = f"{self.valves.searxng_endpoint_url}/search"
                params = {
                    "q": search_intent,
                    "engines": engines_list,
                    "format": "json"
                }
                async with session.get(searxng_url, params=params, timeout=self.valves.crawler_timeout_seconds) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", [])
                        for res in results[:self.valves.max_search_depth]:
                            if res.get("url"):
                                discovered_urls.append({"url": res.get("url"), "title": res.get("title", "")})
                    else:
                        await emit_status(f"SearXNG failed: Status {resp.status}", done=True)
                        return f"<details>\n<summary>Gigascrape Error</summary>\nSearXNG sweep failed with HTTP {resp.status}\n</details>"
        except Exception as e:
            await emit_status(f"SearXNG Error Intercepted.", done=True)
            return f"<details>\n<summary>Gigascrape Error</summary>\nException during SearXNG query: {str(e)}\n</details>"
        
        if not discovered_urls:
            await emit_status("No deep-web metadata found.", done=True)
            return "<details>\n<summary>Gigascrape Result</summary>\nNo URLs discovered for this semantic query within target engines.\n</details>"
        
        extracted_payloads = []
        semaphore = asyncio.Semaphore(5) # Prevent host system memory exhaustion

        async def crawl_url(session, target, index):
            url = target['url']
            title = target['title']
            await emit_status(f"Crawling URL {index} of {len(discovered_urls)} via Chromium", done=False, hidden=False)
            
            # Formulating the custom CrawlerRunConfig for BYPASS and JS extraction
            payload = {
                "urls": url, # Using urls for crawl4ai-proxy standard
                "cache_mode": "BYPASS",
                "word_count_threshold": 50,
                "js_code": "window.scrollTo(0, document.body.scrollHeight);", # js_code_before_wait generic trigger
                "wait_for": "body"
            }
            
            async with semaphore:
                try:
                    # Posting to /crawl on the proxy
                    async with session.post(f"{self.valves.crawl4ai_endpoint_url}/crawl", json=payload, timeout=self.valves.crawler_timeout_seconds) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            
                            # Proxy might return different structures based on specific image versions,
                            # attempting a generic text/markdown extraction grab.
                            markdown_content = str(data)[:3000] # Cap size to prevent prompt bloating 
                            return f"### [Source] {title}\nURL: {url}\n\n```markdown\n{markdown_content}\n```\n"
                        else:
                            await emit_status(f"Playwright/Browserless fault on URL {index}. Proceeding.", done=False, hidden=False)
                            return f"### [Source] {title}\nURL: {url}\n*Crawl failed: HTTP {resp.status}*\n"
                except asyncio.TimeoutError:
                    await emit_status(f"Timeout: URL {index} unreachable. Proceeding.", done=False, hidden=False)
                    return f"### [Source] {title}\nURL: {url}\n*Timeout reached. Anti-bot block or dead link.*\n"
                except Exception as e:
                    await emit_status(f"Exception: URL {index} unreachable. Proceeding.", done=False, hidden=False)
                    return f"### [Source] {title}\nURL: {url}\n*Crawl Exception: {str(e)}*\n"

        async with aiohttp.ClientSession() as session:
            tasks = [crawl_url(session, target, idx + 1) for idx, target in enumerate(discovered_urls)]
            crawled_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for res in crawled_results:
                if isinstance(res, str):
                    extracted_payloads.append(res)
        
        await emit_status("", done=True, hidden=False)
        
        final_output = "\n".join(extracted_payloads)
        
        # Yield a details_block directly as a string to mitigate infinite retry loops 
        return f"<details>\n<summary>Gigascrape Deep-Web Traversal Results</summary>\n\n{final_output}\n</details>"
