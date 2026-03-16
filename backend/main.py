from fastapi import FastAPI
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

load_dotenv()
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)

GEMINI_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-flash-latest",
    "gemini-2.5-flash",
]
_model_index = 0
_model_lock = threading.Lock()

# Remove _get_next_model_name as we're switching to fallback logic


from difflib import SequenceMatcher
from math import exp
from collections import Counter



# Find recent megathread links using Reddit's .json API
def get_recent_megathread_links(subreddit="dubai", thread_title="Attacks Megathread", count=3):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; uae-news-app/0.1)"}
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=90"
    resp = requests.get(url, headers=headers)
    posts = resp.json().get("data", {}).get("children", [])
    links = []
    for post in posts:
        data = post.get("data", {})
        if thread_title.lower() in data.get("title", "").lower():
            links.append(f"https://www.reddit.com{data.get('permalink')}.json")
        if len(links) >= count:
            break
    return links

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

def collect_reddit_raw_comments_last_24h(start_json_url: str, hours: int = 24, max_threads: int = 16) -> List[dict]:
    cutoff = time.time() - hours * 3600
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
def aggregate_reddit_comments_gemini(raw_comments: List[dict]) -> List[dict]:
    """Aggregates Reddit comments with Gemini, considering upvotes and replies."""
    if not raw_comments:
        return []

    # Sort chronologically (oldest first) so Gemini naturally sees the earliest
    # report for each group first, making "first report" identification reliable.
    sorted_comments = sorted(raw_comments, key=lambda c: c.get("timestamp", ""))
    input_data_str = ""
    for c in sorted_comments:
        txt = c.get("text") or ""
        if txt.strip():
            score_info = f", Score: {c.get('score', 1)}" if c.get("score") is not None else ""
            parent_info = f", Parent ID: {c.get('parent_id')}" if c.get('parent_id') else ""
            input_data_str += f"\n---\nID: {c.get('id')}{parent_info}, Timestamp: {c.get('timestamp')}{score_info}, link: {c.get('link')}, body: {txt.strip()[:150]}"

    if not input_data_str:
        return []

    prompt = f"""
        You are a safety incident aggregator for incidents in UAE. Analyze and aggregate relevant user reports
        regarding safety incidents (explosions, interceptions, sirens, drones, etc.) in UAE. 

        Confidence Scoring:
        - Use 'high' if the event is CORROBORATED by multiple independent users OR has a HIGH UPVOTE SCORE (e.g. 5+).
        - Use 'medium' if it's a single specific report or has a few upvotes.
        - Use 'low' if it's vague or lacks detail but still reports an incident.
        
        It is OK to include single, isolated reports if they describe a relevant incident.
        IGNORE purely off-topic chatter, jokes, or irrelevant questions.
        
        Group related reports only if they -
        1. Report similar incidents
        2. Reported at similar times
        3. Reported in the same or nearby areas. For example, do not group together incidents in different emirates, or from far apart areas of Dubai

        Return a list of aggregated validated reports, each with a summary, confidence score,
        safety concern severity score from 1-10, location, and earliest reported timestamp.

        For the timestamp, use the earliest timestamp among the comments in the group.
        Also use that comment's link as the link for the aggregated report.

        Context: The 'Data' below includes ID and Parent ID to show conversation hierarchy. 
        A reply (child) often corroborates or refutes the parent comment. Use this to determine confidence.

        Format the output as a JSON list like:
        [
        {{"location": "DIFC", "coordinates": [25.0805, 55.1411], "incident": "Interception heard", "confidence": "high",
          "summary": "Multiple users reporting...", "severity": 5, "timestamp" : <earliest relevant timestamp str>,
          "link" : <link to earliest comment link>}},
        ]

        Data:
        {input_data_str}
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
            outputs = json.loads(response.text)
            # Ensure ID and source for frontend
            for i, item in enumerate(outputs):
                if not item.get("id"):
                    item["id"] = f"agg_{int(time.time())}_{i}"
                if not item.get("source"):
                    item["source"] = "Reddit Aggregation"
            return outputs
        except Exception as e:
            print(f"Gemini aggregation error with {model_name}: {e}")
            errors.append(f"{model_name}: {str(e)}")
            if "API_KEY_INVALID" in str(e):
                print("CRITICAL: GEMINI_API_KEY is invalid or missing.")
                break # Don't try other models if key is invalid
            continue

    print(f"All Gemini models failed. Errors: {errors}")
    return []

_KNOWN_AREAS = [
    # Dubai
    "jbr", "jlt", "marina", "dubai marina", "downtown", "downtown dubai", "business bay",
    "barsha", "al barsha", "barsha 1", "barsha 2", "barsha 3",
    "deira", "bur dubai", "jumeirah", "jumeirah beach", "satwa", "karama",
    "mirdif", "al quoz", "al qusais", "rashidiya", "nad al sheba", "international city",
    "silicon oasis", "motor city", "sports city", "dic", "dubai internet city", "media city",
    "difc", "dubai international financial centre",
    "jafza", "jebel ali", "jebel ali village", "jaddaf", "al jaddaf",
    "al nahda", "al nahda 1", "al nahda 2",
    # Other emirates/areas commonly referenced
    "sharjah", "ajman", "rak", "ras al khaimah", "umm al quwain", "fujairah", "abu dhabi",
]

_CANONICAL_AREA = {
    "dic": "Dubai Internet City",
    "jbr": "JBR",
    "jlt": "JLT",
    "rak": "Ras Al Khaimah",
    "al jaddaf": "Al Jaddaf",
    "jaddaf": "Al Jaddaf",
    "dip": "Dubai Investment Park",
    "ad": "Abu Dhabi",
    "difc": "DIFC",
    "dubai international financial centre": "DIFC",
}

_DISCOVERED_AREAS_PATH = Path(__file__).resolve().parent / "known_areas_additions.json"
_DISCOVERED_LOCK = threading.Lock()
_discovered_areas: List[str] = []  # lowercase keys we've seen and persisted
_discovered_canonical: dict = {}   # lowercase -> display name

def _load_discovered_areas() -> None:
    global _discovered_areas, _discovered_canonical
    try:
        if not _DISCOVERED_AREAS_PATH.exists():
            return
        data = json.loads(_DISCOVERED_AREAS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            _discovered_areas = [str(x).strip().lower() for x in data if x]
        elif isinstance(data, dict):
            _discovered_areas = [k.strip().lower() for k in data.keys() if k]
            _discovered_canonical = {k.strip().lower(): str(v).strip() for k, v in data.items() if k}
    except Exception:
        pass

def _save_discovered_area(lower_key: str, display_name: str) -> None:
    global _discovered_areas, _discovered_canonical
    with _DISCOVERED_LOCK:
        if lower_key in _discovered_areas or lower_key in _discovered_canonical:
            return
        _discovered_areas.append(lower_key)
        _discovered_canonical[lower_key] = display_name
        try:
            payload = {k: _discovered_canonical.get(k, k.title()) for k in _discovered_areas}
            _DISCOVERED_AREAS_PATH.write_text(json.dumps(payload, indent=0), encoding="utf-8")
        except Exception:
            pass

_load_discovered_areas()

def _all_known_areas() -> List[str]:
    """Known + discovered areas, longest first for matching."""
    combined = list(_KNOWN_AREAS) + [a for a in _discovered_areas if a not in _KNOWN_AREAS]
    return sorted(set(combined), key=len, reverse=True)

def _canonical_for_area(lower_key: str, fallback_display: str) -> str:
    if lower_key in _CANONICAL_AREA:
        return _CANONICAL_AREA[lower_key]
    if lower_key in _discovered_canonical:
        return _discovered_canonical[lower_key]
    return fallback_display

def _extract_location_hint(text: str) -> Optional[str]:
    t = _normalize_text(text)
    tl = t.lower()

    # Terms that indicate a time window or vague reference, not a place.
    _time_like_terms = {
        "hour", "hours", "minute", "minutes", "second", "seconds",
        "last", "today", "tonight", "yesterday", "now", "earlier",
        "morning", "evening", "afternoon", "night",
    }

    # 1) Direct known-area or discovered-area mentions
    for area in _all_known_areas():
        tokens = area.split()
        # Skip entries that are purely time-like phrases we accidentally learned,
        # e.g. "last hour".
        if tokens and all(tok in _time_like_terms for tok in tokens):
            continue
        if re.search(rf"\b{re.escape(area)}\b", tl):
            return _canonical_for_area(area, area.title())

    # 2) "in X", "at X", "near X", "around X", "from X" (X = short place name)
    m = re.search(r"\b(in|at|near|around|from)\s+(?:the\s+)?([A-Za-z0-9][A-Za-z0-9\s\-/']{1,35})", t, re.I)
    if m:
        loc = re.sub(r"[.,;:!?]+$", "", m.group(2).strip()).strip()
        tokens = loc.lower().split()
        if (
            loc
            and 1 <= len(tokens) <= 4
            and not (set(tokens) & _time_like_terms)
            and loc.lower() not in {"dubai", "uae", "here", "there", "my area", "the"}
        ):
            key = loc.lower()
            if key not in _all_known_areas() and key not in _discovered_areas:
                _save_discovered_area(key, loc.title() if loc.isupper() or loc.islower() else loc)
            return _canonical_for_area(key, loc)

    # 3) "X area", "X side" (e.g. "Barsha 1 area", "JBR side")
    m = re.search(r"\b([A-Za-z0-9][A-Za-z0-9\s\-/']{1,30})\s+(?:area|side)\b", t, re.I)
    if m:
        loc = m.group(1).strip()
        if loc and len(loc.split()) <= 4 and loc.lower() not in {"dubai", "uae", "my", "that", "this"}:
            key = loc.lower()
            if key not in _all_known_areas() and key not in _discovered_areas:
                _save_discovered_area(key, loc.title() if loc.isupper() or loc.islower() else loc)
            return _canonical_for_area(key, loc)

    # 4) "heard/saw/... in X" or "reported in X" (keep X short to avoid clauses)
    m = re.search(r"\b(heard|saw|seen|reported|happened|occurred)\b.*?\b(?:in|at|near)\s+(?:the\s+)?([A-Za-z0-9][A-Za-z0-9\s\-/']{1,30})", t, re.I)
    if m:
        loc = re.sub(r"[.,;:!?]+$", "", m.group(2).strip()).strip()
        # Reject long or sentence-like fragments (e.g. "alerts are getting much longer")
        tokens = loc.lower().split()
        if (
            loc
            and 1 <= len(tokens) <= 4
            and not (set(tokens) & _time_like_terms)
            and loc.lower() not in {"dubai", "uae", "here", "there"}
        ):
            skip = {"ad", "getting", "longer", "alerts", "much", "are", "the", "and", "or"}
            if not set(loc.lower().split()) & skip:
                key = loc.lower()
                if key not in _all_known_areas() and key not in _discovered_areas:
                    _save_discovered_area(key, loc.title() if loc.isupper() or loc.islower() else loc)
                return _canonical_for_area(key, loc)

    return None

def _event_type_from_text(text: str) -> str:
    t = _normalize_text(text).lower()
    if re.search(r"\bintercept(ed|ion)?\b", t):
        return "interception"
    if re.search(r"\b(missile|rocket)\b", t):
        return "missile/rocket activity"
    if re.search(r"\b(drone|uav)\b", t):
        return "drone activity"
    if re.search(r"\b(debris|shrapnel|wreckage|impact)\b", t):
        return "debris/impact report"
    if re.search(r"\b(siren|alarm)s?\b", t):
        return "sirens/alarms"
    if re.search(r"\b(boom|bang|blast|explosion)s?\b", t):
        return "explosion/boom heard"
    if re.search(r"\b(sound|noise|rumbling)\b", t):
        return "unusual sound"
    return "reported event"

def _summarize_cluster(cluster: List[dict]) -> str:
    # Pick the most "relevant" comment as representative for event typing + location hint.
    best = max(cluster, key=lambda c: (c.get("_relevance", 0.0), len(c.get("text", ""))))
    rep_text = best.get("text", "") or ""

    event_type = _event_type_from_text(rep_text)

    # Try to pull a location hint from any comment in cluster
    loc = None
    for c in cluster:
        loc = _extract_location_hint(c.get("text", "") or "")
        if loc:
            break

    count = len(cluster)
    if loc:
        return f"{event_type.capitalize()} reported near {loc}. ({count} report{'s' if count != 1 else ''})"
    return f"{event_type.capitalize()} reported. ({count} report{'s' if count != 1 else ''})"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health_check():
    return {"status": "ok", "timestamp": time.time()}

from typing import List, Optional, Union, Any

class NewsItem(BaseModel):
    id: str = ""
    source: str = "Report"
    location: str = "Unknown"
    incident: str = "Event"
    summary: str = ""
    severity: int = 1
    confidence: str = "medium"
    timestamp: str = ""
    coordinates: Any = None
    link: str = ""

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
    print(areas)
    return areas

_CACHE_TTL_SECONDS = 15 * 60  # 15 minutes
_CACHE_VERSION = 5
_CACHE_PATH = Path(__file__).resolve().parent / "cache_news.json"
_NEWS_CACHE: dict = {"ts": 0.0, "data": None, "raw_count": 0}
_CACHE_LOCK = threading.Lock() # Protects access to _NEWS_CACHE
_FETCH_LOCK = threading.Lock() # Ensures only one refresh happens at a time

def _load_news_cache_from_disk() -> None:
    try:
        if not _CACHE_PATH.exists():
            return
        payload = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        if int(payload.get("version", 0) or 0) != _CACHE_VERSION:
            return
        ts = float(payload.get("ts", 0.0) or 0.0)
        data = payload.get("data", None)
        raw_count = int(payload.get("raw_count", 0) or 0)
        if isinstance(data, list):
            _NEWS_CACHE["ts"] = ts
            _NEWS_CACHE["data"] = data
            _NEWS_CACHE["raw_count"] = raw_count
    except Exception:
        # Cache should never break the API.
        return

def _save_news_cache_to_disk(ts: float, data: list, raw_count: int) -> None:
    try:
        _CACHE_PATH.write_text(json.dumps({
            "version": _CACHE_VERSION, 
            "ts": ts, 
            "data": data,
            "raw_count": raw_count
        }), encoding="utf-8")
    except Exception:
        return

def _get_cached_news_entry() -> dict:
    with _CACHE_LOCK:
        return {
            "ts": _NEWS_CACHE.get("ts", 0.0),
            "data": _NEWS_CACHE.get("data"),
            "raw_count": _NEWS_CACHE.get("raw_count", 0)
        }

def _get_cached_news() -> Optional[list]:
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

        print("Checking for enough new data to refresh cache...")
        megathread_links = get_recent_megathread_links()
        
        all_comments = []
        if megathread_links:
            raw_comments = collect_reddit_raw_comments_last_24h(megathread_links[0], hours=24, max_threads=16)
            new_count = len(raw_comments)
            
            # Check growth: only query Gemini if count increased by 10%+ or cache is empty
            growth_threshold = int(last_raw_count * 1.1)
            if current_data is not None and new_count < growth_threshold:
                print(f"Skipping Gemini refresh. New comments ({new_count}) < 110% of last count ({last_raw_count}).")
                # Update ts to delay next check, but keep old data
                _set_cached_news(current_data, last_raw_count) 
                return current_data

            print(f"Executing Gemini aggregation with {new_count} comments (significant growth from {last_raw_count})...")
            # Use qualitative filter: keep comments with upvotes OR replies
            filtered = [c for c in raw_comments if (c.get('score', 1) > 1) or c.get('has_replies', False)]
            
            # Fallback if filter is too restrictive or still too large
            if not filtered and raw_comments:
                filtered = sorted(raw_comments, key=lambda x: (x.get('score', 0), len(x.get('text', ''))), reverse=True)[:30]

            aggregated_comments = aggregate_reddit_comments_gemini(filtered)
            all_comments.extend(aggregated_comments)
            _set_cached_news(all_comments, new_count)
            return all_comments
        
        return []

# Removed background cache updater as it's now triggered by endpoints.

_load_news_cache_from_disk()

def fetch_x_twitter_reports():
    # List of relevant X (Twitter) usernames
    accounts = [
        "wamnews",  # Emirates News Agency
        "TheNationalNews",
        "khaleejtimes",
        "AJENews",  # Al Jazeera English
        "admediaoffice",
        "DXBMediaOffice",
        "modgovae",  # Ministry of Defence
        "moiuae"
    ]
    headers = {
        "Authorization": f"Bearer {TWITTER_BEARER_TOKEN}",
    }
    tweets = []
    for username in accounts:
        url = f"https://api.twitter.com/2/tweets/search/recent?query=from:{username}&tweet.fields=created_at,author_id&max_results=5"
        resp = requests.get(url, headers=headers)
        print(f"Twitter API {username} status: {resp.status_code}")
        # try:
        #     print(f"Response: {resp.json()}")
        # except Exception as e:
        #     print(f"Non-JSON response: {resp.text}")
        if resp.status_code == 200:
            data = resp.json()
            for t in data.get("data", []):
                tweets.append({
                    "id": t["id"],
                    "timestamp": t["created_at"],
                    "source": f"X (@{username})",
                    "category": "Official Alert",
                    "location": "UAE",  # Could use NLP/geotagging for more detail
                    "severity": 4,
                    "text": t["text"],
                    "link": f"https://x.com/{username}/status/{t['id']}"
                })
    return tweets

def fetch_uae_gov_alerts():
    # TODO: Integrate scraping or API for official UAE government alerts
    # For now, return dummy data
    return [
        {
            "id": "gov_shelter_1",
            "source": "UAE Ministry of Interior",
            "location": "Abu Dhabi",
            "incident": "Shelter Alert",
            "summary": "Shelter in place order issued for Abu Dhabi.",
            "severity": 5,
            "confidence": "high",
            "timestamp": str(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=4)))),
            "coordinates": [24.4667, 54.3667],
            "link": "https://twitter.com/moiuae/status/1234567890"
        }
    ]

from fastapi import BackgroundTasks

@app.get("/news", response_model=List[NewsItem])
def get_news(background_tasks: BackgroundTasks):
    cached = _get_cached_news()
    
    # Check if we need to trigger a background refresh
    now = time.time()
    ts = float(_NEWS_CACHE.get("ts", 0.0) or 0.0)
    if cached is None or (now - ts) > _CACHE_TTL_SECONDS:
        print("Triggering background refresh as cache is stale or empty.")
        background_tasks.add_task(_refresh_news_data)

    if cached is not None:
        return [NewsItem(**comment) for comment in cached]

    # Only if cache is absolutely empty and we have no fallback data, 
    # we might wait or return empty list. For now, empty list is safer
    # to maintain responsiveness.
    return []

@app.get("/areas", response_model=List[AreaStatus])
def get_areas(background_tasks: BackgroundTasks):
    # Same stale-while-revalidate logic for areas
    news = get_news(background_tasks)
    raw_items = [n.dict() if isinstance(n, NewsItem) else n for n in news]
    return _build_area_status_from_news(raw_items)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
