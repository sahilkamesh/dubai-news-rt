from fastapi import FastAPI, BackgroundTasks, Response
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional, Union, Any
from pydantic import BaseModel
import requests
import json
import re
import threading
import google.generativeai as genai
import datetime
import time
from pathlib import Path
from dotenv import load_dotenv
import os
import redis

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
REDIS_URL = os.getenv("REDIS_URL")

redis_client = None
if REDIS_URL:
    try:
        redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        print("Successfully configured Redis for caching")
    except Exception as e:
        print(f"Failed to configure Redis: {e}")

genai.configure(api_key=GEMINI_API_KEY)

GEMINI_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemma-3-27b-it",
    "gemini-2.5-flash-lite",
    "gemma-3-12b-it",
    "gemini-2.0-flash",
]

# Find recent megathread links using Reddit's .json API
def get_recent_megathread_links(subreddit="dubai", thread_title="Attacks Megathread", count=3):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; uae-news-app/0.1)"}
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=90"
    try:
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"Reddit API error: {resp.status_code}")
            return []
            
        data = resp.json()
        posts = data.get("data", {}).get("children", [])
        links = []
        for post in posts:
            data_inner = post.get("data", {})
            if thread_title.lower() in data_inner.get("title", "").lower():
                links.append(f"https://www.reddit.com{data_inner.get('permalink')}.json")
            if len(links) >= count:
                break
        return links
    except Exception as e:
        print(f"Error fetching megathread links: {e}")
        return []

def _extract_comments_recursive(children: list, cutoff: float, seen_ids: set, parent_id: Optional[str] = None) -> List[dict]:
    """Helper to traverse nested Reddit comments (replies)."""
    extracted = []
    for child in children:
        if child.get("kind") != "t1":
            continue
        data = child.get("data", {})
        cid = data.get("id")
        if not cid or cid in seen_ids:
            continue
        
        created = float(data.get("created_utc", 0.0) or 0.0)
        if created < cutoff:
            continue
            
        seen_ids.add(cid)
        body = data.get("body", "") or ""
        score = int(data.get("score", 1))
        timestamp = datetime.datetime.fromtimestamp(created, datetime.timezone(datetime.timedelta(hours=4)))
        
        # Check for existence of replies
        replies = data.get("replies")
        has_replies = False
        if isinstance(replies, dict):
            reply_children = replies.get("data", {}).get("children", [])
            has_replies = any(c.get("kind") == "t1" for c in reply_children)
            
        extracted.append({
            "id": cid,
            "parent_id": parent_id,
            "timestamp": str(timestamp),
            "source": "Reddit",
            "score": score,
            "category": "User Report",
            "text": body,
            "link": f"https://reddit.com{data.get('permalink', '')}",
            "author": data.get("author") or "unknown",
            "has_replies": has_replies,
        })
        
        # Recurse into replies
        if has_replies:
            reply_children = replies.get("data", {}).get("children", [])
            extracted.extend(_extract_comments_recursive(reply_children, cutoff, seen_ids, cid))
            
    return extracted

def collect_reddit_raw_comments(start_json_url: str, cutoff: float, max_threads: int = 16) -> List[dict]:
    raw: List[dict] = []
    seen_comment_ids = set()
    thread_url = start_json_url

    for _ in range(max_threads):
        if not thread_url:
            break

        thread_json = _fetch_reddit_thread_json(thread_url, sort="new", limit=500)
        if not thread_json:
            break

        children = thread_json[1].get("data", {}).get("children", [])
        
        # New recursive extraction
        thread_comments = _extract_comments_recursive(children, cutoff, seen_comment_ids)
        raw.extend(thread_comments)

        if children:
            # Check the last top-level comment to decide if we need to jump to previous megathread
            last_top = None
            for c in reversed(children):
                if c.get("kind") == "t1":
                    last_top = c.get("data") or {}
                    break
            
            if last_top:
                oldest_top = float(last_top.get("created_utc", 0.0) or 0.0)
                if oldest_top >= cutoff:
                    thread_url = _extract_previous_megathread_json_url(thread_json)
                    continue

        break

    return raw

# --- Reddit thread traversal (24h window) ---
def _fetch_reddit_thread_json(json_url: str, sort: str = "new", limit: int = 500) -> Optional[list]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; uae-news-app/0.1)"}
    try:
        resp = requests.get(json_url, headers=headers, params={"sort": sort, "limit": limit})
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, list) and len(data) >= 2 else None
    except Exception:
        return None

def _extract_previous_megathread_json_url(thread_json: list) -> Optional[str]:
    """
    Attempts to find a "previous megathread" link in the OP selftext/title.
    Returns a .json URL if found.
    """
    try:
        post = thread_json[0]["data"]["children"][0]["data"]
        title = post.get("title", "") or ""
        selftext = post.get("selftext", "") or ""
    except Exception:
        return None

    hay = f"{title}\n{selftext}"

    # Prefer explicit reddit links.
    # Matches full URLs or /r/... relative links.
    candidates = []
    for m in re.finditer(r"(https?://(?:www\.)?reddit\.com/r/[^)\s]+/comments/[a-z0-9]+/[^)\s]+)", hay, flags=re.I):
        candidates.append(m.group(1))
    for m in re.finditer(r"(/r/[^)\s]+/comments/[a-z0-9]+/[^)\s]+)", hay, flags=re.I):
        candidates.append("https://www.reddit.com" + m.group(1))

    # Heuristic: pick the first that looks like a megathread part link.
    for url in candidates:
        if "megathread" in url.lower() or "attacks" in url.lower():
            return url.rstrip("/") + "/.json"

    return candidates[0].rstrip("/") + "/.json" if candidates else None

'''
AGGREGATION
'''

"""
Aggregates Reddit comments into major report clusters using the Gemini API.
This function sends the relevant comments to Gemini, asks for summary clusters,
and returns a list of dicts representing the aggregated reports.
Requires GEMINI_API_KEY to be set in the environment.
"""
def aggregate_reddit_comments_gemini(raw_comments: List[dict], current_aggregates: List[dict] = None) -> Optional[List[dict]]:
    """
    Aggregates Reddit comments with Gemini.
    Takes new raw comments AND existing aggregated news items to manage incremental updates.
    Returns the FULL list of updated aggregated news items.
    """
    if not raw_comments:
        return current_aggregates or []

    # Prepare existing aggregates for prompt
    existing_data_str = "[]"
    if current_aggregates:
        # Only pass relevant fields to keep prompt concise
        concise_existing = []
        for item in current_aggregates:
            concise_existing.append({
                "id": item.get("id"),
                "location": item.get("location"),
                "incident": item.get("incident"),
                "timestamp": item.get("timestamp"),
                "summary": item.get("summary", "")
            })
        existing_data_str = json.dumps(concise_existing, indent=2)

    # Sort new comments chronologically
    sorted_comments = sorted(raw_comments, key=lambda c: c.get("timestamp", ""))
    input_data_str: str = ""
    for c in sorted_comments:
        txt = c.get("text") or ""
        if txt.strip():
            score_info = f", Score: {c.get('score', 1)}" if c.get("score") is not None else ""
            parent_info = f", Parent ID: {c.get('parent_id')}" if c.get('parent_id') else ""
            input_data_str += f"\n---\nID: {c.get('id')}{parent_info}, Timestamp: {c.get('timestamp')}{score_info}, link: {c.get('link')}, body: {txt.strip()[:150]}"

    if not input_data_str:
        return current_aggregates or []

    prompt = f"""
        You are a safety incident aggregator for incidents in UAE. Your task is to update a list of 
        incident reports based on NEW data while maintaining existing verified incidents.

        ### CURRENT AGGREGATED INCIDENTS (CONTEXT):
        {existing_data_str}

        ### NEW RAW USER REPORTS:
        {input_data_str}

        ### INSTRUCTIONS:
        1. REVIEW: Look at the NEW reports and see if they describe new incidents OR add detail/corroboration to EXISTING incidents.
        2. MERGE/UPDATE: If a new report describes an incident already in the 'CURRENT' list:
           - Update the existing incident's summary with any new details.
           - Update the 'link' if a new report provides a more informative description.
           - DO NOT change the 'id', 'location', or 'timestamp' (keep the earliest timestamp).
        3. ADD NEW: If new reports (at least 2 independent reports OR 1 report with high score/replies) describe a NEW incident:
           - Cluster them into a new incident entry with a location, representative coordinates, a safety severity score (1-10), the earliest reported timestamp, and the most informative source link.
        4. CORROBORATION RULE: ONLY create or update incidents if they are corroborated by multiple people OR high engagement (4+ score or many replies).
        5. NEVER DELETE: The final list MUST include ALL incidents from the 'CURRENT' list, either updated or unchanged. NEVER remove an incident.
        6. IGNORE: Single, isolated reports with no corroboration or relevance.

        Context for Corroboration:
        - A reply (child ID with a Parent ID) often corroborates or adds detail to the parent report.
        - Multiple independent IDs reporting similar symptoms in the same location are the strongest evidence.

        Return a FULL updated JSON list of aggregated reports. Each record MUST have:
        [
        {{"id": "preserve_existing_or_leave_empty_for_new", 
          "location": "DIFC", "coordinates": [25.0805, 55.1411], "incident": "Interception heard",
          "summary": "Multiple users reporting...", "severity": 5, "timestamp" : "earliest timestamp",
          "link" : "most relevant link"}},
        ]
    """

    errors = []
    for model_name in GEMINI_MODELS:
        try:
            print(f"Trying Gemini model: {model_name} with {len(raw_comments)} comments")
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction="Extract security incidents in UAE from Reddit comments. Return ONLY valid JSON."
            )
            response = model.generate_content(
                prompt,
                generation_config={"response_mime_type": "application/json"}
            )
            print("Gemini response:", response.text)
            raw_outputs = json.loads(response.text)
            
            # Post-process: Ensure ID and source for frontend
            results = []
            seen_ids = set()
            
            # Map existing aggregates for easy lookup by ID if LLM didn't return them for some reason
            # (Though instructions say to return ALL)
            existing_map = {item.get("id"): item for item in (current_aggregates or []) if item.get("id")}

            for i, item in enumerate(raw_outputs):
                oid = item.get("id")
                
                # If it's a new item or an update to an existing one
                if not oid or oid.startswith("agg_") is False or oid not in existing_map:
                    # It's a new item (or LLM generated a new ID)
                    if not oid or oid not in seen_ids:
                        item["id"] = oid or f"agg_{int(time.time())}_{i}"
                        item["source"] = item.get("source") or "Reddit Aggregation"
                        results.append(item)
                        seen_ids.add(item["id"])
                else:
                    # It's an update to an existing item
                    # Merge with existing to ensure any fields NOT handled by LLM are preserved
                    existing_item = existing_map[oid]
                    updated_item = {**existing_item, **item}
                    results.append(updated_item)
                    seen_ids.add(oid)

            # Safety check: if LLM ignored Rule 5 (Never Delete), forcefully add back missing ones
            for eid, eitem in existing_map.items():
                if eid not in seen_ids:
                    print(f"Warning: LLM omitted existing incident {eid}. Restoring.")
                    results.append(eitem)

            return results
        except Exception as e:
            print(f"Gemini aggregation error with {model_name}: {e}")
            errors.append(f"{model_name}: {str(e)}")
            if "API_KEY_INVALID" in str(e):
                print("CRITICAL: GEMINI_API_KEY is invalid or missing.")
                break # Don't try other models if key is invalid
            continue

    print(f"All Gemini models failed. Errors: {errors}")
    return None


app = FastAPI()

# CORS Configuration
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
env_frontend = os.getenv("FRONTEND_URL")
if env_frontend:
    ALLOWED_ORIGINS.append(env_frontend)
else:
    # Fallback for development if no FRONTEND_URL is set
    ALLOWED_ORIGINS.append("*")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health_check(response: Response, background_tasks: BackgroundTasks):
    # Allow any origin for the health check endpoint
    response.headers["Access-Control-Allow-Origin"] = "*"
    
    # Trigger a refresh if stale, leveraging the 10-minute pings from GitHub Actions
    now = time.time()
    ts = float(_NEWS_CACHE.get("ts", 0.0) or 0.0)
    if (_NEWS_CACHE.get("data") is None or (now - ts) > _CACHE_TTL_SECONDS) and not _FETCH_LOCK.locked():
        print("Health check triggering background refresh (cache stale/empty).")
        background_tasks.add_task(_refresh_news_data)
        
    return {"status": "ok", "timestamp": now}

from typing import List, Optional, Union, Any

class NewsItem(BaseModel):
    id: str = ""
    source: str = "Report"
    location: str = "Unknown"
    incident: str = "Event"
    summary: str = ""
    severity: int = 1
    timestamp: str = ""
    coordinates: Any = None
    link: str = ""

class NewsResponse(BaseModel):
    news: List[NewsItem]
    last_updated: float

class AreaStatus(BaseModel):
    area: str
    coordinates: Any = None
    severity: int
    lastUpdated: str
    activeAlerts: List[str]


def _build_area_status_from_news(news_items: List[dict]) -> List[AreaStatus]:
    """
    Aggregate per-area status from individual news items.
    Only include items that have a concrete location (not 'Unknown').
    """
    by_area: dict = {}
    for item in news_items:
        loc = (item.get("location") or "").strip()
        if not loc or loc.lower() == "unknown":
            continue

        # Parse timestamp for recency; fall back to 0 on failure.
        ts_str = str(item.get("timestamp") or "")
        try:
            ts_val = datetime.datetime.fromisoformat(ts_str).timestamp()
        except Exception:
            ts_val = 0.0

        sev = int(item.get("severity") or 1)

        # Normalize coordinates: if it's a list of lists, pick the first one.
        coords = item.get("coordinates")
        if isinstance(coords, list) and len(coords) > 0 and isinstance(coords[0], list):
            coords = coords[0]

        entry = by_area.get(loc)
        if not entry:
            by_area[loc] = {
                "area": loc,
                "coordinates": coords or [],
                "severity": sev,
                "last_ts": ts_val,
                "lastUpdated": ts_str,
                "activeAlerts": [str(item.get("summary") or "").strip()] if item.get("summary") else [],
            }
        else:
            # Keep the max severity seen for this area.
            if sev > entry["severity"]:
                entry["severity"] = sev
            # Track the most recent timestamp.
            if ts_val > entry["last_ts"]:
                entry["last_ts"] = ts_val
                entry["lastUpdated"] = ts_str
            # Optionally keep a small set of recent alert texts.
            text = str(item.get("summary") or "").strip()
            if text and text not in entry["activeAlerts"]:
                entry["activeAlerts"].append(text)

    # Convert internal dicts to AreaStatus models
    areas: List[AreaStatus] = []
    for data in by_area.values():
        # Limit active alerts to a few most recent/unique messages
        alerts = data["activeAlerts"][:3]
        areas.append(
            AreaStatus(
                area=data["area"],
                coordinates=data["coordinates"],
                severity=int(data["severity"]),
                lastUpdated=data["lastUpdated"],
                activeAlerts=alerts,
            )
        )
    print("Areas:", areas)
    return areas

_CACHE_TTL_SECONDS = 15 * 60  # 15 minutes
_CACHE_VERSION = 5
_CACHE_PATH = Path(__file__).resolve().parent / "cache_news.json"
_NEWS_CACHE: dict = {"ts": 0.0, "data": None, "raw_count": 0, "last_comment_ts": 0.0}
_CACHE_LOCK = threading.Lock() # Protects access to _NEWS_CACHE
_FETCH_LOCK = threading.Lock() # Ensures only one refresh happens at a time

def _load_news_cache_from_disk() -> None:
    try:
        payload = None
        # Primary source: Redis
        if redis_client:
            try:
                cached_str = redis_client.get("news_cache")
                if cached_str:
                    payload = json.loads(cached_str)
                    print("Loaded news cache from Redis.")
            except Exception as e:
                print(f"Redis load error for news: {e}")
                
        # Secondary source: Fallback to local disk if Redis returned nothing or failed
        if payload is None and _CACHE_PATH.exists():
            try:
                payload = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
                print("Fallback: Loaded news cache from local disk.")
            except Exception as e:
                print(f"Local disk fallback load error: {e}")
            
        if not isinstance(payload, dict):
            return
            
        # Ensure we only load cache if it matches the current version
        if int(payload.get("version", 0) or 0) != _CACHE_VERSION:
            print(f"Cache version mismatch. Expected {_CACHE_VERSION}, got {payload.get('version')}. Refreshing...")
            return
            
        ts = float(payload.get("ts", 0.0) or 0.0)
        data = payload.get("data", None)
        raw_count = int(payload.get("raw_count", 0) or 0)
        last_comment_ts = float(payload.get("last_comment_ts", 0.0) or 0.0)
        
        if isinstance(data, list):
            _NEWS_CACHE["ts"] = ts
            _NEWS_CACHE["data"] = data
            _NEWS_CACHE["raw_count"] = raw_count
            _NEWS_CACHE["last_comment_ts"] = last_comment_ts
    except Exception as e:
        print(f"Cache load generic error: {e}")
        return

def _save_news_cache_to_disk(ts: float, data: list, raw_count: int) -> None:
    try:
        payload = json.dumps({
            "version": _CACHE_VERSION, 
            "ts": ts, 
            "data": data,
            "raw_count": raw_count,
            "last_comment_ts": _NEWS_CACHE.get("last_comment_ts", 0.0)
        })
        
        redis_success = False
        if redis_client:
            try:
                redis_client.set("news_cache", payload)
                redis_success = True
            except Exception as e:
                print(f"Redis save error for news: {e}")
        
        # Only write to local disk if Redis isn't available OR if it failed to save.
        # This keeps the local file as a true emergency fallback and reduces file IO.
        if not redis_success:
            _CACHE_PATH.write_text(payload, encoding="utf-8")
    except Exception as e:
        print(f"Failed to save cache: {e}")
        return

def _get_cached_news_entry() -> dict:
    with _CACHE_LOCK:
        return {
            "ts": _NEWS_CACHE.get("ts", 0.0),
            "data": _NEWS_CACHE.get("data"),
            "raw_count": _NEWS_CACHE.get("raw_count", 0),
            "last_comment_ts": _NEWS_CACHE.get("last_comment_ts", 0.0)
        }

def _get_cached_news() -> Optional[list]:
    """Returns data ONLY if it is fresh. Use _get_cached_news_entry for 'stale-while-revalidate'."""
    entry = _get_cached_news_entry()
    now = time.time()
    if isinstance(entry["data"], list) and (now - entry["ts"]) <= _CACHE_TTL_SECONDS:
        return entry["data"]
    return None

def _set_cached_news(data: list, raw_count: int, ts: Optional[float] = None) -> None:
    if ts is None:
        ts = time.time()
    with _CACHE_LOCK:
        _NEWS_CACHE["ts"] = ts
        _NEWS_CACHE["data"] = data
        _NEWS_CACHE["raw_count"] = raw_count
        # last_comment_ts is updated separately during refresh
    _save_news_cache_to_disk(ts, data, raw_count)

def _refresh_news_data() -> list:
    """
    Internal logic to perform the fetch and aggregation.
    Uses _FETCH_LOCK to ensure only one thread/process does this at a time.
    """
    with _FETCH_LOCK:
        # Double check time-based TTL under lock
        cached_data = _get_cached_news()
        if cached_data is not None:
            return cached_data

        entry = _get_cached_news_entry()
        current_data = entry["data"]
        last_raw_count = entry["raw_count"]

        megathread_links = get_recent_megathread_links()
        
        all_comments = []
        if megathread_links:
            # Use the previous cache timestamp as the cutoff. If it doesn't exist or is 0, fetch last 24h.
            now = time.time()
            cutoff = entry["ts"] if entry["ts"] > 0 else (now - 24 * 3600)
            
            raw_comments = collect_reddit_raw_comments(megathread_links[0], cutoff=cutoff, max_threads=16)
            new_count = len(raw_comments)
            
            # Update the latest comment timestamp seen
            if new_count > 0:
                try:
                    # Find the newest timestamp string among raw comments
                    latest_raw_ts_str = max(c.get("timestamp", "") for c in raw_comments)
                    if latest_raw_ts_str:
                        latest_raw_ts = datetime.datetime.fromisoformat(latest_raw_ts_str).timestamp()
                        with _CACHE_LOCK:
                            if latest_raw_ts > _NEWS_CACHE["last_comment_ts"]:
                                _NEWS_CACHE["last_comment_ts"] = latest_raw_ts
                except Exception as e:
                    print(f"Error updating last_comment_ts: {e}")

            print(f"Executing Gemini aggregation with {new_count} new comments (scheduled refresh)...")
            
            if new_count == 0:
                print("No new comments found. Retaining existing data.")
                _set_cached_news(current_data, last_raw_count, ts=now)
                return current_data if current_data is not None else []

            aggregated_results = aggregate_reddit_comments_gemini(raw_comments, current_data)
            
            if aggregated_results is not None:
                print(f"Gemini refresh successful. Data updated with {new_count} raw comments processed.")
                # We now replacement with the full updated list from Gemini
                new_data = aggregated_results
                
                # Update both in-memory and persistence layers (Redis/Disk)
                _set_cached_news(new_data, last_raw_count + new_count, ts=now)
                return new_data
            else:
                print("Gemini failed to provide a valid response for all models. Retaining stale data.")
                return current_data if current_data is not None else []
        
        return current_data if current_data is not None else []

_load_news_cache_from_disk()

def fetch_x_twitter_reports():
    """
    TODO: Implement real Twitter fetching using designated official accounts.
    Currently returns an empty list as a placeholder.
    """
    return []

def fetch_uae_gov_alerts():
    """
    TODO: Integrate official UAE government alert sources or portal scraping.
    Currently returns an empty list as a placeholder.
    """
    return []

@app.get("/news", response_model=NewsResponse)
def get_news(background_tasks: BackgroundTasks):
    entry = _get_cached_news_entry()
    cached_data = entry["data"]
    fetch_ts = entry["ts"]
    
    # Trigger background refresh if stale or empty AND not already refreshing
    now = time.time()
    if (cached_data is None or (now - fetch_ts) > _CACHE_TTL_SECONDS) and not _FETCH_LOCK.locked():
        print(f"Triggering background refresh. Cache age: {int(now - fetch_ts)}s")
        background_tasks.add_task(_refresh_news_data)

    # ALWAYS return whatever we have in the cache, even if stale.
    if cached_data is not None:
        # Sort by timestamp descending so newest is always first
        sorted_news = sorted(cached_data, key=lambda x: x.get("timestamp", ""), reverse=True)
        
        # Priority: Return the timestamp of the latest parsed comment if we have it
        display_ts = entry.get("last_comment_ts") or fetch_ts
        
        # If no comments have ever been parsed, fall back to newest report
        if display_ts == 0.0 and sorted_news:
            try:
                latest_ts_str = sorted_news[0].get("timestamp", "")
                if latest_ts_str:
                    display_ts = datetime.datetime.fromisoformat(latest_ts_str).timestamp()
            except Exception:
                pass

        return NewsResponse(
            news=[NewsItem(**comment) for comment in sorted_news],
            last_updated=display_ts
        )

    return NewsResponse(news=[], last_updated=0.0)

@app.get("/areas", response_model=List[AreaStatus])
def get_areas(background_tasks: BackgroundTasks):
    # Same stale-while-revalidate logic for areas
    news_resp = get_news(background_tasks)
    news = news_resp.news
    now_ts = time.time()
    recent_items = []
    
    for n in news:
        item = n.dict() if isinstance(n, NewsItem) else n
        try:
            ts_val = datetime.datetime.fromisoformat(item["timestamp"]).timestamp()
            # Only include items from the last 24 hours for the map
            if now_ts - ts_val <= 24 * 3600:
                recent_items.append(item)
        except Exception:
            # Fallback if timestamp parsing fails
            recent_items.append(item)
            
    return _build_area_status_from_news(recent_items)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
