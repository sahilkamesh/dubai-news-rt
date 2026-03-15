from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
from pydantic import BaseModel

import datetime
from dotenv import load_dotenv
import os
import time
from pathlib import Path

load_dotenv()
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Setup Gemini (Free Tier: 1,500 requests per day)
import google.generativeai as genai
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    model_name="gemini-2.5-flash",
    system_instruction="Extract security incidents in UAE from Reddit comments. Return ONLY valid JSON."
)

import requests
import json
import re
import threading


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
        oldest_seen_in_thread = None

        for c in children:
            if c.get("kind") != "t1":
                continue
            cdata = c.get("data", {})
            cid = cdata.get("id")
            if not cid or cid in seen_comment_ids:
                continue
            seen_comment_ids.add(cid)

            created = float(cdata.get("created_utc", 0.0) or 0.0)
            if oldest_seen_in_thread is None or created < oldest_seen_in_thread:
                oldest_seen_in_thread = created

            if created < cutoff:
                # since sort=new, once we hit older-than-cutoff we can keep scanning a bit,
                # but it's fine to just continue and allow older ones to be skipped.
                continue

            body = cdata.get("body", "") or ""
            timestamp = datetime.datetime.fromtimestamp(created, datetime.timezone.utc)
            # Convert to GST
            timestamp += datetime.timedelta(hours=4)
            raw.append({
                "id": cid,
                "timestamp": str(timestamp),
                "source": "Reddit",
                "category": "User Report",
                "text": body,
                "link": f"https://reddit.com{cdata.get('permalink', '')}",
                "author": cdata.get("author") or "unknown",
            })

        # If the oldest comment we saw in this thread is still within cutoff,
        # we might need to traverse to the previous megathread to reach further back.
        if oldest_seen_in_thread is not None and oldest_seen_in_thread >= cutoff:
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
def aggregate_reddit_comments_gemini(raw_comments : List[dict]) -> List[dict]:

    import os
    import requests

    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        # If not set, fallback to no aggregation, just return the comments as is
        return raw_comments

    input_data_str = ""
    for c in raw_comments:
        txt = c.get("text") or ""
        if txt.strip():
            input_data_str += f"\n---\nTimestamp: {c.get('timestamp')}, link: {c.get('link')}, body: {txt.strip()[:1000]}"

    if not input_data_str:
        return []

    # Compose the prompt for Gemini
    prompt = f"""
        Analyze user reports from r/dubai. Only consider comments with real reports of incidents seen or heard,
        and with a location. Ensure to ignore off-topic or irrelevant items or questions. Identify specific 
        areas affected by incidents, and the types of incidents (ex interception seen, interception heard, Missile seen, loud noise etc.)
        Please group related incidents (similar area and time) into distinct event reports, each with a summary,
        a confidence score, a severity score from 1-10, a location, and the earliest time the indicent was reported.
        For each cluster, respond as a JSON object with fields: summary, severity score, timestamp, location, and link to earliest comment.
        Format the output as a JSON list like:
        [
        {{"location": "DIFC", "coordinates": [25.0805, 55.1411], "incident": "Interception heard", "confidence": "high",
          "summary": "Multiple users reporting...", "severity": 5, "timestamp" : <earliest relevant timestamp str>,
          "link" : <link to earliest comment link>}},
        ]

        Data:
        {input_data_str}
    """
    # print(prompt)

    # Call Gemini via PaLM/v1 API (Google generative API)
    api_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt}
            ]
        }]
    }
    # Use JSON mode for 100% parseable output
    response = model.generate_content(
        prompt, 
        generation_config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)


# Fetch comments from a Reddit megathread .json endpoint
_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "so", "to", "of", "in", "on", "at", "for", "with",
    "as", "by", "is", "are", "was", "were", "be", "been", "being", "it", "its", "this", "that", "these", "those",
    "i", "im", "i'm", "me", "my", "we", "our", "you", "your", "they", "them", "their", "he", "she", "his", "her",
    "from", "into", "out", "up", "down", "over", "under", "again", "just", "really", "very", "not", "no", "yes",
    "here", "there", "now", "then", "today", "tonight", "yesterday", "tomorrow",
}

_RELEVANT_KEYWORDS = {
    # Interceptions / air-defense / projectiles
    "intercept", "intercepted", "interception", "airdefense", "air-defense", "iron", "dome", "patriot",
    "missile", "missiles", "rocket", "rockets", "drone", "drones", "uav", "shahed",
    # Impacts / debris / explosions / sounds
    "debris", "shrapnel", "fragment", "fragments", "wreckage", "impact", "hit", "explosion", "explosions",
    "boom", "booms", "blast", "blasts", "bang", "banging", "sirens", "siren", "alarm", "alarms",
    # Visual indicators (keep specific nouns; avoid generic verbs like "seen")
    "streak", "streaks", "trail", "trails", "smoke", "smoky", "fireball", "flash", "tracer", "flare",
    # Hearing / reports
    "heard", "hearing", "sound", "sounds",
    "incoming", "overhead",
}

_NEGATIVE_HINTS = {
    # Common irrelevant megathread content types
    "mods", "moderator", "stickied", "sticky", "rules", "rule", "ban", "banned",
    "politics", "propaganda", "source?", "link?", "rumor", "rumour",
}

_OPINION_OR_META_HINTS = {
    # Opinions / takes / discussion (not event reports)
    "i think", "i feel", "in my opinion", "imo", "imho", "likely", "probably", "prediction", "predict",
    "outcome", "ceasefire", "war", "conflict", "geopolitics", "propaganda",
    # Travel / advice / questions that are usually not reporting an event
    "is it safe", "should i", "can i", "any update", "any news", "what should",
    # Thread/meta
    "megathread", "part", "thread",
    # Unrelated but common in these threads
    "gps spoof", "spoofing",
}

_EVENT_REPORT_PATTERNS = [
    # First-person / direct observations
    r"\b(i|we)\s+(heard|hear|saw|see|seen|spotted|noticed|felt)\b",
    r"\b(heard|hearing)\b.*\b(boom|bang|blast|explosion|sirens?|alarm|noise|sound)\b",
    r"\b(saw|seen|spotted)\b.*\b(missile|rocket|drone|uav|streak|trail|smoke|flash|fireball)\b",
    # General event indicators (even without "I")
    r"\bintercept(ed|ion)?\b",
    r"\b(missile|rocket|drone|uav)\b.*\b(overhead|incoming|flying)\b",
    r"\bdebris\b|\bshrapnel\b|\bimpact\b|\bwreckage\b",
    r"\b(siren|alarm)s?\b",
    r"\b(boom|bang|blast|explosion)s?\b",
]

def _normalize_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def _tokenize(text: str) -> List[str]:
    text = _normalize_text(text).lower()
    # keep words and hyphenated words
    tokens = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", text)
    return [t for t in tokens if t not in _STOPWORDS and len(t) >= 2]

def _relevance_score(text: str) -> float:
    tokens = _tokenize(text)
    if not tokens:
        return 0.0
    c = Counter(tokens)
    score = 0.0
    for kw in _RELEVANT_KEYWORDS:
        if kw in c:
            score += 2.0
    for hint in _NEGATIVE_HINTS:
        if hint in text.lower():
            score -= 1.5
    # Encourage first-person sensory reports
    if re.search(r"\b(i|we)\s+(heard|hear|saw|see|seen|felt)\b", text.lower()):
        score += 1.5
    if re.search(r"\banyone\s+else\b", text.lower()):
        score += 0.5
    # Penalize question-style / opinion-style content
    if "?" in text:
        score -= 1.0
    tl = text.lower()
    for hint in _OPINION_OR_META_HINTS:
        if hint in tl:
            score -= 2.5
    return score

def _is_event_report(text: str) -> bool:
    """
    Keep only reported events (interceptions/attacks/debris/sounds),
    not opinions, predictions, or general Q&A.
    """
    t = _normalize_text(text).lower()

    if not t:
        return False

    # Hard drop: mostly questions/advice seeking
    if t.count("?") >= 2:
        return False
    if re.search(r"\b(is it safe|should i|can i|any update|any news)\b", t):
        return False

    # Must look like an event report
    if any(re.search(p, t) for p in _EVENT_REPORT_PATTERNS):
        return True

    # If it contains multiple strong keywords, allow it even without explicit "I"
    tokens = set(_tokenize(t))
    strong = {"intercept", "intercepted", "interception", "missile", "rocket", "drone", "uav", "explosion", "debris", "sirens", "siren"}
    if len(tokens & strong) >= 2:
        return True

    return False

def _classify_severity(text: str) -> int:
    t = _normalize_text(text).lower()

    # High: seeing missiles/drones or interception visuals
    high_patterns = [
        r"\b(saw|seen|spotted)\b.*\b(missile|rocket|drone|uav)\b",
        r"\b(missile|rocket|drone|uav)\b.*\b(overhead|above|flying|incoming)\b",
        r"\bintercept(ed|ion)?\b",
        r"\bfireball\b|\btracer\b|\bstreak(s)?\b|\btrail(s)?\b",
    ]
    if any(re.search(p, t) for p in high_patterns):
        return 9

    # Debris/impact reports are generally serious even without "loud"
    if re.search(r"\b(debris|shrapnel|wreckage|impact|fragment)s?\b", t):
        return 7

    # Medium: loud nearby interceptions/explosions
    medium_patterns = [
        r"\b(loud|huge|massive)\b.*\b(boom|bang|blast|explosion)\b",
        r"\b(boom|bang|blast|explosion)\b.*\b(close|near|nearby|overhead)\b",
        r"\b(siren|alarm)s?\b",
    ]
    if any(re.search(p, t) for p in medium_patterns):
        return 6

    # Low: distant sounds/rumbling
    low_patterns = [
        r"\b(distant|far|faint)\b.*\b(boom|bang|explosion|rumble)\b",
        r"\bheard\b.*\b(distant|far|faint)\b",
        r"\brumbling\b",
        r"\bwhat\s+is\s+that\s+sound\b",
        r"\bheard\b.*\b(sound|noise)\b",
        r"\b(drone|uav)\b.*\b(sound|noise|buzz)\b",
    ]
    if any(re.search(p, t) for p in low_patterns):
        return 3

    # Generic boom/explosion mention (no distance qualifiers)
    if re.search(r"\b(boom|bang|blast|explosion)s?\b", t):
        return 5

    return 1

def _clamp_severity_1_10(sev: int) -> int:
    try:
        sev = int(sev)
    except Exception:
        return 1
    return max(1, min(10, sev))

def _normalize_location_key(loc: str) -> str:
    loc = _normalize_text(loc).lower()
    loc = re.sub(r"[^a-z0-9\s/-]", "", loc)
    loc = re.sub(r"\s+", " ", loc).strip()
    # normalize common patterns
    loc = loc.replace("dubai ", "")
    return loc

def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0

def _cluster_comments(items: List[dict], similarity_threshold: float = 0.32) -> List[List[dict]]:
    """
    Simple greedy clustering by token-set Jaccard similarity, with a fallback
    to SequenceMatcher for very short texts.
    """
    clusters: List[List[dict]] = []
    centroids: List[set] = []

    for item in items:
        text = item.get("text", "") or ""
        tokens = set(_tokenize(text))

        loc_hint = _extract_location_hint(text)
        loc_key = _normalize_location_key(loc_hint) if loc_hint else ""
        item["_loc_hint"] = loc_hint
        item["_loc_key"] = loc_key

        # Include location tokens to bias clustering toward similar areas
        if loc_key:
            tokens = tokens | {f"loc:{t}" for t in loc_key.split(" ") if t}
        item["_tokens"] = tokens

        best_idx = None
        best_sim = 0.0
        for i, c_tokens in enumerate(centroids):
            sim = _jaccard(tokens, c_tokens)
            if sim < similarity_threshold and (len(tokens) <= 6 or len(c_tokens) <= 6):
                # short messages often tokenize poorly; try character similarity
                sim = max(sim, SequenceMatcher(None, item.get("text", ""), clusters[i][0].get("text", "")).ratio())

            # If both have a location key and it differs, down-weight similarity.
            if loc_key:
                other_loc = clusters[i][0].get("_loc_key", "") or ""
                if other_loc and other_loc != loc_key:
                    sim *= 0.65
            if sim > best_sim:
                best_sim = sim
                best_idx = i

        if best_idx is not None and best_sim >= similarity_threshold:
            clusters[best_idx].append(item)
            # centroid = union of tokens (keeps cluster broad enough)
            centroids[best_idx] = centroids[best_idx] | tokens
        else:
            clusters.append([item])
            centroids.append(tokens)

    return clusters

def _confidence_from_reports(unique_reporters: int, total_reports: int) -> float:
    # Smooth logistic-ish curve so it grows quickly early then saturates near 1.0.
    # Using unique reporters primarily, total reports as a tiny bonus.
    u = max(0, unique_reporters)
    t = max(0, total_reports)
    x = u + 0.25 * max(0, t - u)
    return float(1.0 / (1.0 + exp(-(x - 2.0) / 1.5)))

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

class NewsItem(BaseModel):
    location: str
    incident: str
    summary: str
    severity: int
    confidence: str
    timestamp: str
    coordinates: list[float]
    link: str

class AreaStatus(BaseModel):
    area: str
    coordinates: List[float]
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

        entry = by_area.get(loc)
        if not entry:
            by_area[loc] = {
                "area": loc,
                "coordinates": item.get("coordinates") or [],
                "severity": sev,
                "last_ts": ts_val,
                "lastUpdated": ts_str,
                "activeAlerts": [str(item.get("text") or "").strip()] if item.get("text") else [],
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
            text = str(item.get("text") or "").strip()
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

_CACHE_TTL_SECONDS = 10 * 60
_CACHE_VERSION = 4
_CACHE_PATH = Path(__file__).resolve().parent / "cache_news.json"
_NEWS_CACHE: dict = {"ts": 0.0, "data": None}

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
        if isinstance(data, list):
            _NEWS_CACHE["ts"] = ts
            _NEWS_CACHE["data"] = data
    except Exception:
        # Cache should never break the API.
        return

def _save_news_cache_to_disk(ts: float, data: list) -> None:
    try:
        _CACHE_PATH.write_text(json.dumps({"version": _CACHE_VERSION, "ts": ts, "data": data}), encoding="utf-8")
    except Exception:
        return

def _get_cached_news() -> Optional[list]:
    now = time.time()
    ts = float(_NEWS_CACHE.get("ts", 0.0) or 0.0)
    data = _NEWS_CACHE.get("data", None)
    if isinstance(data, list) and (now - ts) <= _CACHE_TTL_SECONDS:
        return data
    return None

def _set_cached_news(data: list) -> None:
    ts = time.time()
    _NEWS_CACHE["ts"] = ts
    _NEWS_CACHE["data"] = data
    _save_news_cache_to_disk(ts, data)

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
            "id": "gov1",
            "timestamp": str(datetime.datetime.now()),
            "source": "UAE Ministry of Interior",
            "category": "Shelter Alert",
            "location": "Abu Dhabi",
            "severity": 5,
            "text": "Shelter in place order issued for Abu Dhabi.",
            "link": "https://twitter.com/moiuae/status/1234567890"
        }
    ]

@app.get("/news", response_model=List[NewsItem])
def get_news():
    cached = _get_cached_news()
    if cached is not None:
        return [NewsItem(**comment) for comment in cached]

    # Collect and aggregate last 24 hours of reported events.
    # megathread_links = get_recent_megathread_links(count=1)
    megathread_links = get_recent_megathread_links()
    print(f"Found reddit megathread links: {megathread_links}")

    all_comments = []
    if megathread_links:
        raw = collect_reddit_raw_comments_last_24h(megathread_links[0], hours=24, max_threads=16)
        aggregated_comments = aggregate_reddit_comments_gemini(raw)
        all_comments.extend(aggregated_comments)
    # Add X (Twitter) and official sources
    # all_comments.extend(fetch_x_twitter_reports())
    # all_comments.extend(fetch_uae_gov_alerts())
    # print(f"Comments: {all_comments}")
    # return [NewsItem(id=i+1, **item) for i, item in enumerate(all_comments)]
    # Cache the processed results for the next 10 minutes.
    _set_cached_news(all_comments)
    return [NewsItem(**comment) for comment in all_comments]

@app.get("/areas", response_model=List[AreaStatus])
def get_areas():
    # Derive per-area status from the same news items that power the feed.
    # This reuses caching logic inside get_news() so we don't refetch unnecessarily.
    news = get_news()
    # get_news() returns a List[NewsItem]; convert to plain dicts for aggregation.
    raw_items = [n.dict() if isinstance(n, NewsItem) else n for n in news]
    return _build_area_status_from_news(raw_items)
