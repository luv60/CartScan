import os
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = r"D:\DevCache\ms-playwright"
os.environ["HF_HOME"] = r"D:\DevCache\huggingface"
os.environ["TORCH_HOME"] = r"D:\DevCache\torch"

import asyncio
import sys

# Playwright launches Chromium through asyncio subprocesses. On Windows,
# SelectorEventLoop does not implement subprocess_exec(), which causes
# Playwright to fail with NotImplementedError. Force the Proactor loop
# before Uvicorn/reload creates the application event loop.
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except AttributeError:
        # Kept for Python versions where the policy name is unavailable.
        pass
import hashlib
import json
import logging
import re
import socket
import threading
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import List, Dict, Any, Optional, Union, Tuple
from fastapi import FastAPI, Query, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from curl_cffi import requests
import sqlite3
from thefuzz import fuzz
import rapidfuzz
import unicodedata
from rapidfuzz import fuzz as rf_fuzz
from seleniumbase import SB, Driver
from starlette.concurrency import run_in_threadpool
import redis
import redis.asyncio as aioredis
from cachetools import LRUCache, TTLCache
from blinkit_service import (
    fetch_blinkit_products,
    fetch_blinkit_products_streaming,
    get_blinkit_serviceability_state,
)
from instamart_service import (
    fetch_instamart_products_streaming,
    fetch_instamart_products,
)
from zepto_service import (
    fetch_zepto_products,
    resolve_zepto_store_id,
    get_zepto_serviceability_state,
    zepto_pool,
    request_zepto_otp,
    validate_zepto_otp,
    AuthPhoneRequest,
    AuthOtpRequest,
)

def strip_accents(text: str) -> str:
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", str(text))
        if unicodedata.category(c) != "Mn"
    )

CANONICAL_BRANDS = {
    "amul": "AMUL",
    "britannia": "BRITANNIA",
    "mother dairy": "MOTHER DAIRY",
    "nandini": "NANDINI",
    "epigamia": "EPIGAMIA",
    "coca cola": "COCA-COLA",
    "coca-cola": "COCA-COLA",
    "coke": "COCA-COLA",
    "pepsi": "PEPSI",
    "nestle": "NESTLE",
    "nescafe": "NESTLE",
    "nescafé": "NESTLE",
    "everyday": "NESTLE",
    "tata": "TATA",
    "tata sampann": "TATA",
    "tata tea": "TATA",
    "fortune": "FORTUNE",
    "aashirvaad": "AASHIRVAAD",
    "aashirwad": "AASHIRVAAD",
    "dabur": "DABUR",
    "parle": "PARLE",
    "cadbury": "CADBURY",
    "haldiram": "HALDIRAM",
    "haldiram's": "HALDIRAM",
    "saffola": "SAFFOLA",
    "lays": "LAYS",
    "lay's": "LAYS",
    "kurkure": "KURKURE",
    "maggi": "MAGGI",
    "kissan": "KISSAN",
    "thums up": "THUMS UP",
    "thumps up": "THUMS UP",
    "sprite": "SPRITE",
    "frooti": "FROOTI",
    "maaza": "MAAZA",
    "red bull": "RED BULL",
    "dettol": "DETTOL",
    "colgate": "COLGATE",
    "surf excel": "SURF EXCEL",
    "ariel": "ARIEL",
    "vim": "VIM",
}


def normalize_quantity(text: Any, packsize: str = "") -> str:
    """
    Unit & Quantity Normalizer:
    Standardize metric values to uppercase canonical tokens:
    - 0.5 kg / 500 gm / 500g -> 500G
    - 1 L / 1000 ml / 1L -> 1000ML
    - 1 pc / 1 pack -> 1PC
    Supports multipacks (e.g., Pack of 2 x 500ml -> 1000ML, 2 x 500ml -> 1000ML).
    Accepts string, dictionary, or title + packsize.
    """
    if isinstance(text, dict):
        t_val = text.get("name") or text.get("title") or text.get("product_name") or ""
        p_val = text.get("packsize") or text.get("pack_size") or text.get("quantity") or packsize
        combined = f"{t_val} {p_val}".strip()
    else:
        combined = f"{text or ''} {packsize or ''}".strip()

    combined = strip_accents(combined)
    c_lower = combined.lower()

    # Multipack multiplier x sub-quantity (e.g. 2 x 500ml, Pack of 2 x 500ml, 3 x 100g)
    m_mult = re.search(
        r'(?:pack\s*of\s*)?(\d+)\s*[xX*]\s*(\d+(?:\.\d+)?)\s*(kg|kilo|kilogram|kgs|g|gm|gms|gram|grams|l|ltr|litre|litres|liter|liters|ml|milli|mls)\b',
        combined,
        re.IGNORECASE
    )
    if m_mult:
        mult = float(m_mult.group(1))
        val = float(m_mult.group(2))
        u = m_mult.group(3).lower()
        if u in ['kg', 'kilo', 'kilogram', 'kgs']:
            tot = mult * val * 1000.0
            return f"{int(tot)}G" if tot.is_integer() else f"{tot}G"
        elif u in ['g', 'gm', 'gms', 'gram', 'grams']:
            tot = mult * val
            return f"{int(tot)}G" if tot.is_integer() else f"{tot}G"
        elif u in ['l', 'ltr', 'litre', 'litres', 'liter', 'liters']:
            tot = mult * val * 1000.0
            return f"{int(tot)}ML" if tot.is_integer() else f"{tot}ML"
        elif u in ['ml', 'milli', 'mls']:
            tot = mult * val
            return f"{int(tot)}ML" if tot.is_integer() else f"{tot}ML"

    # Multiplier from Pack of N or Buy 1 Get 1
    multiplier = 1.0
    m_pack_of = re.search(r'\bpack\s*of\s*(\d+)\b', c_lower)
    if m_pack_of:
        multiplier = float(m_pack_of.group(1))
    elif "buy 1 get 1" in c_lower or "bogo" in c_lower or "buy 1 get 1 free" in c_lower:
        multiplier = 2.0

    # Kilograms -> Convert to base grams (G)
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:kg|kilo|kilogram|kgs)\b', c_lower)
    if m:
        tot = float(m.group(1)) * 1000.0 * multiplier
        return f"{int(tot)}G" if tot.is_integer() else f"{tot}G"

    # Liters -> Convert to base milliliters (ML)
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:l|ltr|litre|litres|liter|liters)\b', c_lower)
    if m:
        tot = float(m.group(1)) * 1000.0 * multiplier
        return f"{int(tot)}ML" if tot.is_integer() else f"{tot}ML"

    # Milliliters -> ML
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:ml|milli|mls|millilitre|millilitres|milliliter)\b', c_lower)
    if m:
        tot = float(m.group(1)) * multiplier
        return f"{int(tot)}ML" if tot.is_integer() else f"{tot}ML"

    # Grams -> G
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:gm|gms|grams?|g)\b', c_lower)
    if m:
        tot = float(m.group(1)) * multiplier
        return f"{int(tot)}G" if tot.is_integer() else f"{tot}G"

    # Pieces / Packs / Units -> PC
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:pc|pcs|piece|pieces|pack|packs|unit|units)\b', c_lower)
    if m:
        tot = float(m.group(1)) * multiplier
        return f"{int(tot)}PC" if tot.is_integer() else f"{tot}PC"

    if multiplier > 1.0:
        return f"{int(multiplier)}PC" if multiplier.is_integer() else f"{multiplier}PC"

    return "1PC"


normalize_unit_and_quantity = normalize_quantity


def decompose_entity(item: Any, packsize: str = "") -> Tuple[str, str, str]:
    """
    Hard-Lock Entity Decomposition:
    Extracts (Brand, Normalized Quantity, Core Product Name) with accent stripping and canonical mapping.
    """
    if isinstance(item, dict):
        title = item.get("name") or item.get("title") or item.get("product_name") or ""
        pack = item.get("packsize") or item.get("pack_size") or item.get("quantity") or packsize
        explicit_brand = item.get("brand") or ""
    else:
        title = str(item or "")
        pack = packsize
        explicit_brand = ""

    t_clean = strip_accents(title.strip())
    t_lower = t_clean.lower()
    norm_qty = normalize_quantity(title, pack)

    brand = ""
    brand_match_str = ""

    # 1. Check explicit brand
    if explicit_brand:
        b_clean = strip_accents(explicit_brand).strip().lower()
        if b_clean in CANONICAL_BRANDS:
            brand = CANONICAL_BRANDS[b_clean]
        else:
            brand = b_clean.upper()
        brand_match_str = explicit_brand.lower()

    # 2. Canonical brand lookup in title
    if not brand:
        for b_key in sorted(CANONICAL_BRANDS.keys(), key=lambda x: len(x), reverse=True):
            b_key_clean = strip_accents(b_key).lower()
            m = re.search(r'\b' + re.escape(b_key_clean) + r'\b', t_lower)
            if m:
                brand = CANONICAL_BRANDS[b_key]
                brand_match_str = m.group(0)
                break

    # 3. Fallback to first non-generic token
    if not brand:
        tokens = re.split(r'[\s\-_|()]+', t_clean)
        for tok in tokens:
            tok_lower = tok.lower()
            if tok and len(tok) > 1 and tok_lower not in ["pack", "fresh", "pure", "original", "natural", "best", "the"]:
                brand = tok.upper()
                brand_match_str = tok_lower
                break

    # 4. Residual Core Product Name extraction
    core = t_lower
    if brand_match_str:
        core = re.sub(r'\b' + re.escape(brand_match_str) + r'\b', ' ', core)

    core = re.sub(r'\b\d+(\.\d+)?\s*(kg|kilo|kilogram|kgs|g|gm|gms|grams?|l|ltr|litre|litres|liter|liters|ml|milli|mls|pc|pcs|piece|pieces|pack|packs|unit|units)\b', ' ', core)
    core = re.sub(r'\bpack\s*of\s*\d+\b', ' ', core)
    core = re.sub(r'\b\d+\s*[xX*]\s*\d+(\.\d+)?\s*\w+\b', ' ', core)
    core = re.sub(r'\bbuy\s*1\s*get\s*1\b', ' ', core)
    core = re.sub(r'\b(tetra\s*pack|tetrapack|tetra|pouch|pet\s*bottle|bottle|glass\s*jar|jar|can|tin|box|carton)\b', ' ', core)
    core = re.sub(r'\b(fresh|pure|homogenised|pasteurised|original|natural)\b', ' ', core)
    core = re.sub(r'[|\-_/\\(),.:;+*&]', ' ', core)
    core = re.sub(r'\b\d+\b', ' ', core)

    tokens = [tok for tok in core.split() if len(tok) > 1]
    tokens.sort()
    core_clean = " ".join(tokens)

    return brand, norm_qty, core_clean


def match_products(item_a: Any, item_b: Any, pack_a: str = "", pack_b: str = "") -> Optional[float]:
    """
    Hard-Lock Entity Matcher:
    - Rule 1 (Quantity Lock): If normalize_quantity(item_a) != normalize_quantity(item_b), return None (0.0).
    - Rule 2 (Brand Lock): Strip accents/casing. If brand tokens mismatch (e.g., "Amul" vs "Mother Dairy"), return None.
    - Rule 3 (Residual Fuzzy Match): Apply rapidfuzz.fuzz.token_sort_ratio ONLY on remaining product tokens after removing brand and quantity.
    - Require a score >= 85%. Return None if under 85%.
    """
    brand_a, qty_a, core_a = decompose_entity(item_a, pack_a)
    brand_b, qty_b, core_b = decompose_entity(item_b, pack_b)

    # Rule 1 (Quantity Lock)
    if qty_a != qty_b:
        return None

    # Rule 2 (Brand Lock): Strip accents/casing and ensure brand tokens match
    if not brand_a or not brand_b or brand_a != brand_b:
        return None

    # Rule 3 (Residual Fuzzy Match): Score strictly on residual product tokens
    if not core_a and not core_b:
        score = 100.0
    elif not core_a or not core_b:
        score = 0.0
    else:
        score = float(rapidfuzz.fuzz.token_sort_ratio(core_a, core_b))

    if score < 85.0:
        return None

    return round(score, 2)


def strict_product_matcher(title_a: Any, title_b: Any, pack_a: str = "", pack_b: str = "") -> bool:
    return match_products(title_a, title_b, pack_a, pack_b) is not None

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("aggregator")


# ---------------------------------------------------------------------------
# SEARCH JOB MANAGER
# ---------------------------------------------------------------------------
# A search job lets the API return immediately while Blinkit and Zepto scrape
# their complete result sets in the background. Provider results are buffered
# in RAM and exposed in batches of at most 50 products.
#
# This is intentionally in-memory for the first implementation. Redis can be
# added later if multiple backend processes/servers are required.
# ---------------------------------------------------------------------------

SEARCH_JOBS: Dict[str, Dict[str, Any]] = {}
SEARCH_JOB_TTL_SECONDS = 15 * 60
SEARCH_JOB_BATCH_SIZE = 50
SEARCH_JOB_INITIAL_COMPARISON_GRACE_SECONDS = 45.0
SEARCH_JOB_LOCK = asyncio.Lock()
SEARCH_JOB_COMPARISON_WAIT_SECONDS = 4.0
SEARCH_JOB_COMPARISON_POLL_SECONDS = 0.10

# TEMPORARY DEBUG EXPORT — OFF by default.
# Set to True only while collecting a controlled multi-search dataset.
DEBUG_EXPORT_PROVIDER_JSON = True
DEBUG_EXPORT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "debug_provider_exports",
)


def _debug_safe_export_name(value: Any, fallback: str = "search") -> str:
    """Convert query text into a safe Windows folder-name component."""
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    return (text[:80] or fallback)


def _debug_export_provider_json(job: Dict[str, Any]) -> None:
    """
    TEMPORARY: save one completed search into its own timestamped folder.

    Example:
        debug_provider_exports/
          2026-09-24_02-50-12_lays/
            metadata.json
            blinkit.json
            zepto.json
            instamart.json

    This function only reads the already-collected provider buffers. It does
    not modify scraper output, matching, batching, or the Flutter response.
    """
    if not DEBUG_EXPORT_PROVIDER_JSON:
        return

    os.makedirs(DEBUG_EXPORT_DIR, exist_ok=True)

    exports = {
        "blinkit": list(job.get("blinkit", {}).get("items") or []),
        "zepto": list(job.get("zepto", {}).get("items") or []),
        "instamart": list(job.get("instamart", {}).get("items") or []),
    }

    # Each search gets a unique folder so later searches never overwrite
    # earlier JSON files.
    created_at = float(job.get("created_at") or time.time())
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(created_at))
    query_name = _debug_safe_export_name(job.get("query"), "search")
    base_name = f"{timestamp}_{query_name}"

    export_dir = os.path.join(DEBUG_EXPORT_DIR, base_name)

    # A second-level suffix protects against two searches created in the same
    # second with the same query.
    suffix = 1
    while os.path.exists(export_dir):
        export_dir = os.path.join(DEBUG_EXPORT_DIR, f"{base_name}_{suffix}")
        suffix += 1

    os.makedirs(export_dir, exist_ok=True)

    for provider_name, items in exports.items():
        path = os.path.join(export_dir, f"{provider_name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2, default=str)

    metadata = {
        "job_id": str(job.get("job_id") or ""),
        "query": str(job.get("query") or ""),
        "latitude": job.get("latitude"),
        "longitude": job.get("longitude"),
        "created_at": created_at,
        "created_at_iso": time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime(created_at)
        ),
        "exported_at": time.time(),
        "provider_counts": {
            provider_name: len(items)
            for provider_name, items in exports.items()
        },
        "provider_status": {
            provider_name: (job.get(provider_name) or {}).get("status", "unknown")
            for provider_name in exports
        },
        "provider_serviceability": {
            provider_name: (job.get(provider_name) or {}).get(
                "serviceability", "unknown"
            )
            for provider_name in exports
        },
    }

    with open(
        os.path.join(export_dir, "metadata.json"),
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2, default=str)

    logger.info(
        "[DEBUG EXPORT] saved search snapshot | query=%r | blinkit=%d | zepto=%d | instamart=%d | dir=%s",
        job.get("query"),
        len(exports["blinkit"]),
        len(exports["zepto"]),
        len(exports["instamart"]),
        export_dir,
    )


def _new_search_job(
    query: str,
    latitude: float,
    longitude: float,
    previous_job_id: Optional[str] = None,
) -> str:
    job_id = uuid.uuid4().hex

    SEARCH_JOBS[job_id] = {
        "job_id": job_id,
        "query": query,
        "latitude": float(latitude),
        "longitude": float(longitude),
        "created_at": time.time(),
        "last_access": time.time(),
        "status": "running",
        "cancel_requested": False,

        # Provider lifecycle.
        "blinkit": {
            "status": "pending",
            "items": [],
            "error": None,
            "serviceability": "unknown",
            "serviceability_message": None,
            "finished_at": None,
            "ready": False,
            "first_ready_at": None,
        },
        "zepto": {
            "status": "pending",
            "items": [],
            "error": None,
            "serviceability": "unknown",
            "serviceability_message": None,
            "finished_at": None,
            "ready": False,
            "first_ready_at": None,
        },
        "instamart": {
            "status": "pending",
            "items": [],
            "error": None,
            "serviceability": "unknown",
            "serviceability_message": None,
            "finished_at": None,
            "ready": False,
            "first_ready_at": None,
        },

        # Background asyncio tasks.
        "tasks": {},

        # Comparison cache. Provider callbacks invalidate this cache whenever
        # new products arrive, so repeated batch requests do not rerun the
        # full matching algorithm unnecessarily.
        "comparison_cards": [],
        "comparison_dirty": True,
        "comparison_built_at": None,

        # Prepared comparison batches. These are the ONLY comparison cards
        # that the Flutter client is allowed to receive.
        #
        # Non-final batches always contain exactly 50 cards. The final batch
        # may contain fewer than 50 cards.
        "comparison_batches": [],
        "comparison_released_card_keys": set(),
        "comparison_finalized": False,
        "comparison_prepare_task": None,

        # Arrival-ordered comparison queue.
        "comparison_event_queue": [],
        "comparison_event_sequence": 0,
        "comparison_event_sequence_by_key": {},
        "comparison_seen_provider_keys": {"blinkit": set(), "zepto": set(), "instamart": set()},
        "comparison_pending_cards": [],
        "comparison_released_provider_keys": set(),

        # Compatibility/debug fields; no longer used to gate page release.
        "comparison_window_size": 0,
        "comparison_next_window_size": SEARCH_JOB_BATCH_SIZE,
        "comparison_initial_release_deadline": time.time() + SEARCH_JOB_INITIAL_COMPARISON_GRACE_SECONDS,
    }

    if previous_job_id:
        previous = SEARCH_JOBS.get(previous_job_id)
        if previous and previous.get("status") == "running":
            previous["cancel_requested"] = True
            for task in list(previous.get("tasks", {}).values()):
                if task and not task.done():
                    task.cancel()
            previous["status"] = "cancelled"

    return job_id


def _cleanup_search_jobs() -> None:
    """Remove completed/failed/cancelled jobs that have exceeded their TTL."""
    now = time.time()
    stale_ids = []

    for job_id, job in list(SEARCH_JOBS.items()):
        last_access = float(job.get("last_access") or job.get("created_at") or now)
        status = job.get("status")

        if status in {"completed", "failed", "cancelled"}:
            if now - last_access > SEARCH_JOB_TTL_SECONDS:
                stale_ids.append(job_id)

    for job_id in stale_ids:
        job = SEARCH_JOBS.pop(job_id, None)
        if not job:
            continue

        for task in list(job.get("tasks", {}).values()):
            if task and not task.done():
                task.cancel()


def _get_search_job(job_id: str) -> Dict[str, Any]:
    _cleanup_search_jobs()

    job = SEARCH_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Search job not found or expired.")

    job["last_access"] = time.time()
    return job


def _normalize_search_provider_items(provider_name: str, raw_items: Any) -> List[Any]:
    """
    Normalize provider scraper output into the raw product list expected by
    the search-job buffer. The provider scrapers themselves are left unchanged.

    Some local versions of the Blinkit service return a response dictionary
    (for example {"products": [...]}) while Zepto currently returns a plain
    list. The old /api/compare path already handles the provider-specific
    response shape, so the job layer must accept both shapes too.
    """
    if isinstance(raw_items, list):
        return raw_items

    if isinstance(raw_items, dict):
        # Common wrapper keys used by the provider service.
        for key in ("products", "items", "results", "data"):
            candidate = raw_items.get(key)
            if isinstance(candidate, list):
                return candidate

        # A few response wrappers nest the actual list under data.
        data = raw_items.get("data")
        if isinstance(data, dict):
            for key in ("products", "items", "results"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    return candidate

    logger.warning(
        "[SEARCH JOB] %s provider returned an unsupported result shape: %s",
        provider_name,
        type(raw_items).__name__,
    )
    return []


def _job_provider_summary(provider: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": provider.get("status", "pending"),
        "ready": bool(provider.get("ready")),
        "count": len(provider.get("items") or []),
        "error": provider.get("error"),
        "serviceability": provider.get("serviceability", "unknown"),
        "serviceability_message": provider.get("serviceability_message"),
        "finished_at": provider.get("finished_at"),
        "first_ready_at": provider.get("first_ready_at"),
    }


def _refresh_search_job_status(job: Dict[str, Any]) -> None:
    if job.get("status") == "cancelled":
        return

    blinkit_status = job["blinkit"].get("status")
    zepto_status = job["zepto"].get("status")
    instamart_status = job["instamart"].get("status")

    provider_statuses = {blinkit_status, zepto_status, instamart_status}

    if provider_statuses == {"completed"}:
        job["status"] = "completed"
    elif provider_statuses == {"failed"}:
        job["status"] = "failed"
    elif "running" in provider_statuses or "completed" in provider_statuses or "failed" in provider_statuses:
        job["status"] = "running"
    else:
        job["status"] = "running"


async def _get_cached_canonical_cards(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build canonical comparison cards only when provider data changed."""
    if not job.get("comparison_dirty"):
        return list(job.get("comparison_cards") or [])

    # The provider tasks and API handlers all run on the same asyncio loop, so
    # this short synchronous build cannot be concurrently mutated by another
    # coroutine. We snapshot the provider buffers before building.
    blinkit_items = list(job["blinkit"].get("items") or [])
    zepto_items = list(job["zepto"].get("items") or [])
    instamart_items = list(job["instamart"].get("items") or [])

    canonical_cards = build_canonical_cards_with_instamart(
        blinkit_items,
        zepto_items,
        instamart_items,
        blinkit_serviceability=job["blinkit"].get("serviceability", "unknown"),
        zepto_serviceability=job["zepto"].get("serviceability", "unknown"),
        instamart_serviceability=job["instamart"].get("serviceability", "unknown"),
    )

    job["comparison_cards"] = canonical_cards
    job["comparison_dirty"] = False
    job["comparison_built_at"] = time.time()
    return list(canonical_cards)


def _invalidate_comparison_cache(job: Dict[str, Any]) -> None:
    job["comparison_dirty"] = True


def _comparison_providers_finished(job: Dict[str, Any]) -> bool:
    """Return True when both provider jobs have reached a terminal state."""
    terminal_states = {"completed", "failed", "cancelled"}
    return all(
        job[name].get("status") in terminal_states
        for name in ("blinkit", "zepto", "instamart")
    )


def _comparison_initial_provider_window_ready(job: Dict[str, Any]) -> bool:
    """
    Decide when the *first* 50-card comparison batch may be frozen.

    Zepto is intentionally a blocking dependency for the initial snapshot
    because its server-side scraper does not stream products into the job; it
    publishes its first usable set only when ``fetch_zepto_products`` returns.
    If we release batch 0 before that happens, Blinkit/Instamart cards become
    immutable for that batch and late Zepto matches can no longer be attached.

    Blinkit and Instamart may continue filling the pending queue while we wait.
    We release as soon as Zepto has products (or reaches a terminal state). A
    safety deadline prevents a broken/hung Zepto provider from blocking a search
    forever.
    """
    deadline = float(job.get("comparison_initial_release_deadline") or 0.0)
    if deadline and time.time() >= deadline:
        logger.warning(
            "[SEARCH JOB %s] initial comparison grace expired; releasing without Zepto",
            str(job.get("job_id") or "")[:8],
        )
        return True

    zepto = job.get("zepto") or {}
    zepto_count = len(zepto.get("items") or [])
    zepto_status = zepto.get("status", "pending")

    # Do not let fast Instamart/Blinkit results consume batch 0 before Zepto
    # has had its first chance to contribute.
    if zepto_count > 0:
        return True
    if zepto_status in {"completed", "failed", "cancelled"}:
        return True

    return False


def _comparison_card_key(card: Dict[str, Any]) -> str:
    """
    Generate a stable key so a canonical comparison card is never released
    twice across prepared batches.
    """
    if not isinstance(card, dict):
        return ""

    blinkit = card.get("blinkit") or {}
    zepto = card.get("zepto") or {}
    instamart = card.get("instamart") or {}

    blinkit_id = str(
        blinkit.get("id")
        or blinkit.get("product_id")
        or ""
    ).strip()

    zepto_id = str(
        zepto.get("id")
        or zepto.get("product_id")
        or ""
    ).strip()

    instamart_id = str(
        instamart.get("id")
        or instamart.get("product_id")
        or ""
    ).strip()

    if blinkit_id or zepto_id or instamart_id:
        return f"provider:{blinkit_id}|{zepto_id}|{instamart_id}"

    canonical_name = str(card.get("canonical_name") or "").strip().lower()
    brand = str(card.get("brand") or "").strip().lower()
    quantity = str(card.get("quantity") or "").strip()
    unit = str(card.get("unit") or "").strip().lower()

    return f"canonical:{canonical_name}|{brand}|{quantity}|{unit}"


def _comparison_unreleased_cards(
    job: Dict[str, Any],
    canonical_cards: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return canonical cards that have not already been released."""
    released_keys = job.setdefault("comparison_released_card_keys", set())
    remaining = []

    for card in canonical_cards:
        key = _comparison_card_key(card)
        if key and key in released_keys:
            continue
        remaining.append(card)

    return remaining


def _comparison_freeze_next_full_batch(
    job: Dict[str, Any],
    canonical_cards: List[Dict[str, Any]],
) -> bool:
    """
    Freeze exactly one complete 50-card comparison batch.

    Never creates a partial batch.
    """
    remaining = _comparison_unreleased_cards(job, canonical_cards)

    if len(remaining) < SEARCH_JOB_BATCH_SIZE:
        return False

    batch = remaining[:SEARCH_JOB_BATCH_SIZE]
    released_keys = job.setdefault("comparison_released_card_keys", set())

    for card in batch:
        key = _comparison_card_key(card)
        if key:
            released_keys.add(key)

    job.setdefault("comparison_batches", []).append(batch)

    logger.info(
        "[SEARCH JOB %s] comparison batch %d READY | cards=%d",
        job["job_id"][:8],
        len(job["comparison_batches"]) - 1,
        len(batch),
    )
    return True


def _comparison_freeze_final_batch(
    job: Dict[str, Any],
    canonical_cards: List[Dict[str, Any]],
) -> bool:
    """
    Freeze remaining cards into 50-card batches, with only the last batch
    allowed to be smaller than 50.
    """
    remaining = _comparison_unreleased_cards(job, canonical_cards)

    if not remaining:
        return False

    released_keys = job.setdefault("comparison_released_card_keys", set())
    added = False

    while len(remaining) >= SEARCH_JOB_BATCH_SIZE:
        batch = remaining[:SEARCH_JOB_BATCH_SIZE]

        for card in batch:
            key = _comparison_card_key(card)
            if key:
                released_keys.add(key)

        job.setdefault("comparison_batches", []).append(batch)
        added = True

        logger.info(
            "[SEARCH JOB %s] comparison batch %d READY | cards=%d",
            job["job_id"][:8],
            len(job["comparison_batches"]) - 1,
            len(batch),
        )

        remaining = _comparison_unreleased_cards(
            job,
            canonical_cards,
        )

    if remaining:
        for card in remaining:
            key = _comparison_card_key(card)
            if key:
                released_keys.add(key)

        job.setdefault("comparison_batches", []).append(remaining)
        added = True

        logger.info(
            "[SEARCH JOB %s] FINAL comparison batch READY | batch=%d cards=%d",
            job["job_id"][:8],
            len(job["comparison_batches"]) - 1,
            len(remaining),
        )

    return added


def _comparison_provider_item_key(provider_name: str, item: Dict[str, Any]) -> str:
    if not isinstance(item, dict): return ""
    value = str(item.get("product_id") or item.get("id") or item.get("sku_id") or item.get("productId") or "").strip()
    if value: return f"{provider_name}:{value}"
    name = str(item.get("name") or item.get("title") or "").strip().lower()
    pack = str(item.get("packsize") or "").strip().lower()
    price = str(item.get("price") or "").strip()
    return f"{provider_name}:fallback:{name}|{pack}|{price}"

def _comparison_card_provider_keys(card: Dict[str, Any]) -> set:
    """Return every provider product key represented by a comparison card.

    A canonical card can keep additional products in ``*_variants``. Those
    products are still part of the card and must participate in queue identity
    and arrival ordering.
    """
    keys = set()
    if not isinstance(card, dict):
        return keys

    for provider_name in ("blinkit", "zepto", "instamart"):
        primary = card.get(provider_name)
        if isinstance(primary, dict):
            key = _comparison_provider_item_key(provider_name, primary)
            if key:
                keys.add(key)

        variants = card.get(f"{provider_name}_variants") or []
        if isinstance(variants, list):
            for item in variants:
                if not isinstance(item, dict):
                    continue
                key = _comparison_provider_item_key(provider_name, item)
                if key:
                    keys.add(key)

    return keys


def _comparison_card_arrival_sequence(job: Dict[str, Any], card: Dict[str, Any]) -> int:
    """Return the earliest provider-arrival sequence represented by a card."""
    event_sequence_by_key = job.setdefault("comparison_event_sequence_by_key", {})
    sequences = [
        int(event_sequence_by_key[key])
        for key in _comparison_card_provider_keys(card)
        if key in event_sequence_by_key
    ]
    return min(sequences) if sequences else 10**12


def _comparison_queue_provider_items(job: Dict[str, Any], provider_name: str, items: List[Dict[str, Any]]) -> None:
    seen = job.setdefault("comparison_seen_provider_keys", {}).setdefault(provider_name, set())
    events = job.setdefault("comparison_event_queue", [])
    sequence_by_key = job.setdefault("comparison_event_sequence_by_key", {})

    for item in items or []:
        if not isinstance(item, dict):
            continue

        key = _comparison_provider_item_key(provider_name, item)
        if not key or key in seen:
            continue

        seen.add(key)
        job["comparison_event_sequence"] = int(job.get("comparison_event_sequence") or 0) + 1
        sequence = job["comparison_event_sequence"]
        sequence_by_key[key] = sequence
        events.append({
            "sequence": sequence,
            "provider": provider_name,
            "item": item,
        })


def _comparison_find_candidate_for_event(
    job: Dict[str, Any],
    provider_name: str,
    item: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build the canonical card represented by one newly arrived product.

    The provider item is the arrival trigger. We still use the complete
    opposite-provider buffer so an already available cross-store match can be
    attached immediately, but we do NOT wait for either scraper to finish.
    """
    if not isinstance(item, dict):
        return None

    if provider_name == "blinkit":
        blinkit_items = [item]
        zepto_items = list(job["zepto"].get("items") or [])
    else:
        blinkit_items = list(job["blinkit"].get("items") or [])
        zepto_items = [item]

    if not blinkit_items and not zepto_items:
        return None

    # Run the existing matcher against a snapshot. This helper is called from
    # the async comparison worker, so use the thread for CPU-heavy matching.
    import copy
    blinkit_snapshot = copy.deepcopy(blinkit_items)
    zepto_snapshot = copy.deepcopy(zepto_items)

    # This function itself stays synchronous so the queue worker can process
    # events in strict arrival order. The actual async wrapper below performs
    # the matching in a worker thread.
    return None


async def _comparison_find_candidate_for_event_async(
    job: Dict[str, Any],
    provider_name: str,
    item: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None

    # Only the triggering item from the same provider is needed. Passing the
    # entire same-provider buffer here makes a 72-item Instamart first batch
    # rebuild all 72 items for every event (O(N^2)), blocking batch release.
    provider_buffers = {
        "blinkit": list(job["blinkit"].get("items") or []),
        "zepto": list(job["zepto"].get("items") or []),
        "instamart": list(job["instamart"].get("items") or []),
    }
    provider_buffers[provider_name] = [item]

    import copy
    snapshots = {
        name: copy.deepcopy(items)
        for name, items in provider_buffers.items()
    }

    canonical_cards = await asyncio.to_thread(
        build_canonical_cards_with_instamart,
        snapshots["blinkit"],
        snapshots["zepto"],
        snapshots["instamart"],
        job["blinkit"].get("serviceability", "unknown"),
        job["zepto"].get("serviceability", "unknown"),
        job["instamart"].get("serviceability", "unknown"),
    )

    target = _comparison_provider_item_key(provider_name, item)
    if not target:
        return None

    for card in canonical_cards:
        if target in _comparison_card_provider_keys(card):
            return card

    return None


def _comparison_enqueue_candidate(job: Dict[str, Any], card: Dict[str, Any]) -> bool:
    """Add/update one canonical card in the pending mixed queue."""
    if not isinstance(card, dict):
        return False

    pending = job.setdefault("comparison_pending_cards", [])
    released_provider = job.setdefault("comparison_released_provider_keys", set())
    keys = _comparison_card_provider_keys(card)

    if not keys or keys & released_provider:
        return False

    # A later event may produce a richer representation of the same family.
    # Replace it in-place so the queue never contains the same comparison card
    # twice before it is released.
    for idx, existing in enumerate(pending):
        if keys & _comparison_card_provider_keys(existing):
            pending[idx] = card
            return True

    pending.append(card)
    return True

def _comparison_release_ready_pending_batches(job: Dict[str, Any]) -> bool:
    pending = job.setdefault("comparison_pending_cards", [])
    released_keys = job.setdefault("comparison_released_card_keys", set())
    released_provider = job.setdefault("comparison_released_provider_keys", set())
    added = False

    while len(pending) >= SEARCH_JOB_BATCH_SIZE:
        batch = pending[:SEARCH_JOB_BATCH_SIZE]
        del pending[:SEARCH_JOB_BATCH_SIZE]

        for card in batch:
            key = _comparison_card_key(card)
            if key:
                released_keys.add(key)
            released_provider.update(_comparison_card_provider_keys(card))

        job.setdefault("comparison_batches", []).append(batch)
        added = True

        logger.info(
            "[SEARCH JOB %s] comparison batch %d READY | cards=%d | pending=%d",
            job["job_id"][:8],
            len(job["comparison_batches"]) - 1,
            len(batch),
            len(pending),
        )

    return added


async def _prepare_comparison_batches(job_id: str) -> None:
    """Prepare mixed 50-card comparison pages as provider data arrives."""
    while True:
        job = SEARCH_JOBS.get(job_id)
        if not job or job.get("cancel_requested"):
            return

        try:
            events = job.setdefault("comparison_event_queue", [])

            if events:
                events.sort(key=lambda e: int(e.get("sequence") or 0))
                queued_events = events[:]
                del events[:]

                merged = 0
                for event in queued_events:
                    provider = event.get("provider")
                    item = event.get("item")
                    if provider not in {"blinkit", "zepto", "instamart"} or not isinstance(item, dict):
                        continue

                    try:
                        card = await _comparison_find_candidate_for_event_async(
                            job, provider, item
                        )
                        if card and _comparison_enqueue_candidate(job, card):
                            merged += 1
                    except Exception as event_exc:
                        # One malformed provider SKU must never kill the shared
                        # comparison worker. Drop only this event and continue
                        # processing the remaining Blinkit/Zepto/Instamart queue.
                        logger.exception(
                            "[SEARCH JOB %s] comparison event skipped | provider=%s | error=%s",
                            job["job_id"][:8], provider, type(event_exc).__name__,
                        )

                    # During the first comparison window, wait until all
                    # providers have contributed (or the grace deadline fires)
                    # so late Zepto/Instamart products can enrich Blinkit
                    # cards before the first 50 are permanently released.
                    if (
                        job.get("comparison_batches")
                        or _comparison_initial_provider_window_ready(job)
                    ):
                        _comparison_release_ready_pending_batches(job)

                logger.info(
                    "[SEARCH JOB %s] comparison event processing | provider_events=%d | current_blinkit=%d | current_zepto=%d | current_instamart=%d | cards_added=%d | pending=%d | released_batches=%d",
                    job["job_id"][:8],
                    len(queued_events),
                    len(job["blinkit"].get("items") or []),
                    len(job["zepto"].get("items") or []),
                    len(job["instamart"].get("items") or []),
                    merged,
                    len(job.get("comparison_pending_cards") or []),
                    len(job.get("comparison_batches") or []),
                )

            if _comparison_providers_finished(job):
                # Drain any final provider events before finalizing.
                final_events = job.setdefault("comparison_event_queue", [])
                final_events.sort(key=lambda e: int(e.get("sequence") or 0))
                queued_final = final_events[:]
                del final_events[:]
                for event in queued_final:
                    provider = event.get("provider")
                    item = event.get("item")
                    if provider not in {"blinkit", "zepto", "instamart"} or not isinstance(item, dict):
                        continue
                    try:
                        card = await _comparison_find_candidate_for_event_async(job, provider, item)
                        if card:
                            _comparison_enqueue_candidate(job, card)
                    except Exception as event_exc:
                        logger.exception(
                            "[SEARCH JOB %s] final comparison event skipped | provider=%s | error=%s",
                            job["job_id"][:8], provider, type(event_exc).__name__,
                        )
                    _comparison_release_ready_pending_batches(job)

                pending = job.setdefault("comparison_pending_cards", [])
                released_keys = job.setdefault("comparison_released_card_keys", set())
                released_provider = job.setdefault("comparison_released_provider_keys", set())

                if pending:
                    final_batch = list(pending)
                    pending.clear()
                    for card in final_batch:
                        key = _comparison_card_key(card)
                        if key:
                            released_keys.add(key)
                        released_provider.update(_comparison_card_provider_keys(card))

                    job.setdefault("comparison_batches", []).append(final_batch)
                    logger.info(
                        "[SEARCH JOB %s] FINAL comparison batch READY | batch=%d cards=%d",
                        job["job_id"][:8],
                        len(job["comparison_batches"]) - 1,
                        len(final_batch),
                    )

                job["comparison_finalized"] = True
                return

            await asyncio.sleep(SEARCH_JOB_COMPARISON_POLL_SECONDS)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[SEARCH JOB %s] comparison preparation iteration failed",
                job_id[:8],
            )
            await asyncio.sleep(SEARCH_JOB_COMPARISON_POLL_SECONDS)


async def _run_search_provider(
    job_id: str,
    provider_name: str,
) -> None:
    """Run one provider while exposing usable products as soon as possible."""
    job = SEARCH_JOBS.get(job_id)
    if not job:
        return

    provider = job[provider_name]
    provider["status"] = "running"

    try:
        query = job["query"]
        lat = float(job["latitude"])
        lon = float(job["longitude"])

        logger.info(
            "[SEARCH JOB %s] %s provider started | query=%r | lat=%s | lon=%s",
            job_id[:8], provider_name, query, lat, lon,
        )

        if provider_name == "blinkit":
            async def on_first_batch(items):
                current_job = SEARCH_JOBS.get(job_id)
                if not current_job or current_job.get("cancel_requested"):
                    return
                normalized = _normalize_search_provider_items("blinkit", items)
                if normalized:
                    current_job["blinkit"]["items"] = normalized
                    current_job["blinkit"]["ready"] = True
                    current_job["blinkit"]["first_ready_at"] = time.time()
                    _comparison_queue_provider_items(current_job, "blinkit", normalized)
                    _invalidate_comparison_cache(current_job)
                    _refresh_search_job_status(current_job)
                    logger.info(
                        "[SEARCH JOB %s] blinkit FIRST BATCH ready | %d products",
                        job_id[:8], len(normalized),
                    )

            async def on_background_batch(items):
                current_job = SEARCH_JOBS.get(job_id)
                if not current_job or current_job.get("cancel_requested"):
                    return
                normalized = _normalize_search_provider_items("blinkit", items)
                if not normalized:
                    return

                existing = current_job["blinkit"].setdefault("items", [])
                seen = {
                    str(x.get("product_id"))
                    for x in existing
                    if isinstance(x, dict) and x.get("product_id")
                }
                for item in normalized:
                    pid = str(item.get("product_id") or "") if isinstance(item, dict) else ""
                    if pid and pid in seen:
                        continue
                    existing.append(item)
                    if pid:
                        seen.add(pid)

                _comparison_queue_provider_items(current_job, "blinkit", normalized)
                _invalidate_comparison_cache(current_job)

                logger.info(
                    "[SEARCH JOB %s] blinkit background batch | added=%d total=%d",
                    job_id[:8], len(normalized), len(existing),
                )

            async def on_serviceability(serviceable: bool):
                current_job = SEARCH_JOBS.get(job_id)
                if not current_job:
                    return
                current_provider = current_job["blinkit"]
                current_provider["serviceability"] = (
                    "serviceable" if serviceable else "unserviceable"
                )
                current_provider["serviceability_message"] = (
                    "Blinkit is serviceable at this location."
                    if serviceable
                    else "Blinkit is not serviceable at this location."
                )
                logger.info(
                    "[SEARCH JOB %s] blinkit serviceability=%s | message=%s",
                    job_id[:8],
                    current_provider["serviceability"],
                    current_provider["serviceability_message"],
                )

            items = await fetch_blinkit_products_streaming(
                query,
                lat,
                lon,
                on_first_batch=on_first_batch,
                on_background_batch=on_background_batch,
                on_serviceability=on_serviceability,
            )

            blinkit_serviceability = get_blinkit_serviceability_state()
            provider["serviceability"] = blinkit_serviceability.get("status", "unknown")
            provider["serviceability_message"] = blinkit_serviceability.get("message")
        elif provider_name == "zepto":
            items = await fetch_zepto_products(query, lat, lon)
            logger.info(
                "[SEARCH JOB %s] zepto FIRST COMPLETE RESULT | %d products",
                job_id[:8], len(items or []),
            )
            serviceability = get_zepto_serviceability_state()
            provider["serviceability"] = serviceability.get("status", "unknown")
            provider["serviceability_message"] = serviceability.get("message")
            logger.info(
                "[SEARCH JOB %s] zepto serviceability=%s | message=%s",
                job_id[:8],
                provider["serviceability"],
                provider["serviceability_message"],
            )
        elif provider_name == "instamart":
            async def on_first_batch(items):
                current_job = SEARCH_JOBS.get(job_id)
                if not current_job or current_job.get("cancel_requested"):
                    return
                normalized = _normalize_search_provider_items("instamart", items)
                if not normalized:
                    return
                current_job["instamart"]["items"] = normalized
                current_job["instamart"]["ready"] = True
                current_job["instamart"]["first_ready_at"] = time.time()
                _comparison_queue_provider_items(current_job, "instamart", normalized)
                _invalidate_comparison_cache(current_job)
                _refresh_search_job_status(current_job)
                logger.info(
                    "[SEARCH JOB %s] instamart FIRST BATCH ready | %d products",
                    job_id[:8], len(normalized),
                )

            async def on_background_batch(items):
                current_job = SEARCH_JOBS.get(job_id)
                if not current_job or current_job.get("cancel_requested"):
                    return
                normalized = _normalize_search_provider_items("instamart", items)
                if not normalized:
                    return
                existing = current_job["instamart"].setdefault("items", [])
                seen = {
                    str(x.get("product_id"))
                    for x in existing
                    if isinstance(x, dict) and x.get("product_id")
                }
                added = 0
                for product in normalized:
                    pid = str(product.get("product_id") or product.get("id") or "")
                    if pid and pid in seen:
                        continue
                    existing.append(product)
                    if pid:
                        seen.add(pid)
                    added += 1
                _comparison_queue_provider_items(current_job, "instamart", normalized)
                _invalidate_comparison_cache(current_job)
                logger.info(
                    "[SEARCH JOB %s] instamart background batch | added=%d total=%d",
                    job_id[:8], added, len(existing),
                )

            async def on_serviceability(state: str):
                current_job = SEARCH_JOBS.get(job_id)
                if not current_job:
                    return
                value = str(state or "unknown").strip().lower()
                if value not in {"serviceable", "unserviceable", "unknown"}:
                    value = "unknown"
                current_job["instamart"]["serviceability"] = value
                current_job["instamart"]["serviceability_message"] = (
                    "Instamart is serviceable at this location."
                    if value == "serviceable"
                    else "Instamart is not serviceable at this location."
                    if value == "unserviceable"
                    else "Instamart serviceability could not be established."
                )
                logger.info(
                    "[SEARCH JOB %s] instamart serviceability=%s | message=%s",
                    job_id[:8], value, current_job["instamart"]["serviceability_message"],
                )

            items = await fetch_instamart_products_streaming(
                query,
                lat,
                lon,
                on_first_batch=on_first_batch,
                on_background_batch=on_background_batch,
                on_serviceability=on_serviceability,
            )
        else:
            raise RuntimeError(f"Unknown search provider: {provider_name}")

        if job.get("cancel_requested"):
            provider["status"] = "cancelled"
            return

        normalized_final = _normalize_search_provider_items(provider_name, items)

        # Blinkit's streaming callbacks have already populated the buffer.
        # Reconcile with the final return without replacing valid streamed data.
        if provider_name == "blinkit":
            if normalized_final:
                existing = provider.get("items") or []
                seen = {
                    str(x.get("product_id"))
                    for x in existing
                    if isinstance(x, dict) and x.get("product_id")
                }
                for item in normalized_final:
                    pid = str(item.get("product_id") or "") if isinstance(item, dict) else ""
                    if pid and pid in seen:
                        continue
                    existing.append(item)
                    if pid:
                        seen.add(pid)
                provider["items"] = existing
                _comparison_queue_provider_items(job, provider_name, existing)
                _invalidate_comparison_cache(job)
            if provider["items"]:
                provider["ready"] = True
        else:
            provider["items"] = normalized_final
            _comparison_queue_provider_items(job, provider_name, normalized_final)
            _invalidate_comparison_cache(job)
            if normalized_final:
                provider["ready"] = True
                provider["first_ready_at"] = provider.get("first_ready_at") or time.time()

        provider["status"] = "completed"
        provider["finished_at"] = time.time()

        logger.info(
            "[SEARCH JOB %s] %s provider completed | %d products",
            job_id[:8], provider_name, len(provider["items"]),
        )

    except asyncio.CancelledError:
        provider["status"] = "cancelled"
        logger.info(
            "[SEARCH JOB %s] %s provider task cancelled",
            job_id[:8], provider_name,
        )
        raise

    except Exception as exc:
        if provider_name == "blinkit":
            serviceability = get_blinkit_serviceability_state()
            provider["serviceability"] = serviceability.get("status", "unknown")
            provider["serviceability_message"] = serviceability.get("message")
        elif provider_name == "zepto":
            serviceability = get_zepto_serviceability_state()
            provider["serviceability"] = serviceability.get("status", "unknown")
            provider["serviceability_message"] = serviceability.get("message")
        elif provider_name == "instamart":
            # Instamart exposes serviceability through the location callback.
            # Keep the last known state instead of replacing it after a
            # background-page error.
            pass

        # Do not erase a valid first batch if a later background page fails.
        if provider.get("items"):
            provider["error"] = f"background collection error: {type(exc).__name__}: {exc}"
            provider["status"] = "completed"
            provider["finished_at"] = time.time()
            logger.exception(
                "[SEARCH JOB %s] %s background collection failed after usable data",
                job_id[:8], provider_name,
            )
        else:
            provider["items"] = []
            provider["error"] = f"{type(exc).__name__}: {exc}"
            provider["status"] = "failed"
            provider["finished_at"] = time.time()
            logger.exception(
                "[SEARCH JOB %s] %s provider failed",
                job_id[:8], provider_name,
            )

    finally:
        job = SEARCH_JOBS.get(job_id)
        if job:
            _refresh_search_job_status(job)


async def _run_search_job(job_id: str) -> None:
    """
    Start Blinkit + Zepto + Instamart concurrently.

    We do NOT wait for one provider before starting another.
    Each provider streams into the same arrival-ordered comparison queue.
    """
    job = SEARCH_JOBS.get(job_id)
    if not job:
        return

    blinkit_task = asyncio.create_task(
        _run_search_provider(job_id, "blinkit"),
        name=f"quickcompare-{job_id}-blinkit",
    )
    zepto_task = asyncio.create_task(
        _run_search_provider(job_id, "zepto"),
        name=f"quickcompare-{job_id}-zepto",
    )
    instamart_task = asyncio.create_task(
        _run_search_provider(job_id, "instamart"),
        name=f"quickcompare-{job_id}-instamart",
    )

    job["tasks"]["blinkit"] = blinkit_task
    job["tasks"]["zepto"] = zepto_task
    job["tasks"]["instamart"] = instamart_task

    # Comparison batching runs independently from Flutter scrolling.
    # It prepares complete 50-card pages as provider data becomes available.
    comparison_prepare_task = asyncio.create_task(
        _prepare_comparison_batches(job_id),
        name=f"quickcompare-{job_id}-comparison-builder",
    )
    job["comparison_prepare_task"] = comparison_prepare_task
    job["tasks"]["comparison"] = comparison_prepare_task

    # Keep this task alive until both provider tasks have finished. The
    # provider tasks themselves update SEARCH_JOBS as soon as each finishes.
    results = await asyncio.gather(
        blinkit_task,
        zepto_task,
        instamart_task,
        return_exceptions=True,
    )

    job = SEARCH_JOBS.get(job_id)
    if not job:
        return

    if job.get("status") != "cancelled":
        _refresh_search_job_status(job)

    # TEMPORARY DEBUG EXPORT — disabled unless DEBUG_EXPORT_PROVIDER_JSON=True.
    # Runs once per search job, after all provider buffers are complete.
    _debug_export_provider_json(job)

    # Provider tasks are finished here. The comparison preparation task should
    # normally have finalized itself; if it is still waiting, give it a chance
    # to finish from the now-terminal provider state.
    comparison_task = job.get("comparison_prepare_task")
    if comparison_task and not comparison_task.done():
        try:
            await asyncio.wait_for(comparison_task, timeout=1.0)
        except asyncio.TimeoutError:
            comparison_task.cancel()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "[SEARCH JOB %s] comparison preparation task failed during shutdown",
                job_id[:8],
            )

    logger.info(
        "[SEARCH JOB %s] job finished | status=%s | blinkit=%d | zepto=%d | instamart=%d",
        job_id[:8],
        job.get("status"),
        len(job["blinkit"].get("items") or []),
        len(job["zepto"].get("items") or []),
        len(job["instamart"].get("items") or []),
    )


def _search_job_public_status(job: Dict[str, Any]) -> Dict[str, Any]:
    _refresh_search_job_status(job)

    return {
        "job_id": job["job_id"],
        "query": job["query"],
        "status": job["status"],
        "created_at": job["created_at"],
        "cancel_requested": bool(job.get("cancel_requested")),
        "batch_size": SEARCH_JOB_BATCH_SIZE,

        "providers": {
            "blinkit": _job_provider_summary(job["blinkit"]),
            "zepto": _job_provider_summary(job["zepto"]),
            "instamart": _job_provider_summary(job["instamart"]),
        },

        # Convenience fields for the Flutter polling layer.
        "blinkit_ready": bool(job["blinkit"].get("ready")),
        "zepto_ready": bool(job["zepto"].get("ready")),
        "instamart_ready": bool(job["instamart"].get("ready")),
        "blinkit_count": len(job["blinkit"].get("items") or []),
        "zepto_count": len(job["zepto"].get("items") or []),
        "instamart_count": len(job["instamart"].get("items") or []),

        # Comparison batches are the units exposed to Flutter.
        "comparison_batches_ready": len(job.get("comparison_batches") or []),
        "comparison_finalized": bool(job.get("comparison_finalized")),
    }



# In-memory RAM caches to serve repeat lookup queries directly from RAM
RAM_DARK_STORE_CACHE = LRUCache(maxsize=1024)
RAM_IMAGE_HASH_CACHE = LRUCache(maxsize=4096)
RAM_STORE_QUERY_CACHE = TTLCache(maxsize=2048, ttl=300)

# Cache TTL constants
DARK_STORE_TTL = 86400      # 24 hours
IMAGE_HASH_TTL = 604800     # 7 days
STORE_CACHE_TTL = 300       # 5 minutes (300 seconds)

# Canonical SKU Registry (SQLite + in-memory O(1) dictionary)
CANONICAL_MAPPINGS: Dict[Tuple[str, str], str] = {}
CANONICAL_DB_PATH = os.path.join(os.path.dirname(__file__), "canonical_registry.db")


def init_canonical_db():
    try:
        conn = sqlite3.connect(CANONICAL_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS canonical_mappings (
                zepto_item_id TEXT NOT NULL,
                blinkit_item_id TEXT NOT NULL,
                canonical_sku_id TEXT NOT NULL,
                PRIMARY KEY (zepto_item_id, blinkit_item_id)
            )
        """)
        conn.commit()
        cursor.execute("SELECT zepto_item_id, blinkit_item_id, canonical_sku_id FROM canonical_mappings")
        for z_id, b_id, c_id in cursor.fetchall():
            CANONICAL_MAPPINGS[(z_id, b_id)] = c_id
            CANONICAL_MAPPINGS[(b_id, z_id)] = c_id
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to initialize canonical SKU registry DB: {e}")


init_canonical_db()


def register_canonical_mapping(zepto_item_id: str, blinkit_item_id: str, canonical_sku_id: str):
    """
    Registers verified (zepto_item_id, blinkit_item_id) -> canonical_sku_id mapping.
    Stored both in-memory for O(1) lookups and persisted to SQLite.
    """
    z_id = str(zepto_item_id).strip()
    b_id = str(blinkit_item_id).strip()
    c_id = str(canonical_sku_id).strip()
    if not (z_id and b_id and c_id):
        return
    CANONICAL_MAPPINGS[(z_id, b_id)] = c_id
    CANONICAL_MAPPINGS[(b_id, z_id)] = c_id
    try:
        conn = sqlite3.connect(CANONICAL_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO canonical_mappings (zepto_item_id, blinkit_item_id, canonical_sku_id)
            VALUES (?, ?, ?)
        """, (z_id, b_id, c_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to persist canonical mapping to SQLite: {e}")


def lookup_canonical_mapping(zepto_item_id: str, blinkit_item_id: str) -> Optional[str]:
    """
    O(1) lookup against verified canonical mappings.
    Returns canonical_sku_id if verified mapping exists, else None.
    """
    z_id = str(zepto_item_id).strip()
    b_id = str(blinkit_item_id).strip()
    if not (z_id and b_id):
        return None
    return CANONICAL_MAPPINGS.get((z_id, b_id)) or CANONICAL_MAPPINGS.get((b_id, z_id))


async_redis_client: Optional[aioredis.Redis] = None
redis_client: Optional[redis.Redis] = None
_redis_last_check: float = 0.0
_redis_check_interval: float = 60.0  # Retry connecting at most once per minute


def get_sync_redis_client() -> Optional[redis.Redis]:
    global redis_client, _redis_last_check
    now = time.time()
    if redis_client is not None:
        return redis_client
    if now - _redis_last_check < _redis_check_interval:
        return None

    _redis_last_check = now
    try:
        # Fast socket probe before initiating redis client connection pool
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.05)
        err = sock.connect_ex(('127.0.0.1', 6379))
        sock.close()
        if err != 0:
            redis_client = None
            return None

        r = redis.Redis(
            host="127.0.0.1",
            port=6379,
            db=0,
            decode_responses=True,
            socket_connect_timeout=0.1,
            socket_timeout=0.1
        )
        r.ping()
        redis_client = r
    except Exception:
        redis_client = None
    return redis_client


def get_redis_client() -> Optional[redis.Redis]:
    return get_sync_redis_client()


class DualTierCacheStr(str):
    """
    Dual-tier string that functions immediately as a standard Python str (for synchronous callers/tests)
    and can also be awaited in async coroutines to ensure async Redis operations complete.
    """
    def __new__(cls, sync_val: str, async_coro_fn):
        instance = super().__new__(cls, sync_val)
        instance._async_coro_fn = async_coro_fn
        return instance

    def __await__(self):
        return self._async_coro_fn().__await__()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Pre-load SentenceTransformer('bge-small-en-v1.5') into app.state.embedding_model
    logger.info("Lifespan: Pre-loading SentenceTransformer('bge-small-en-v1.5')...")
    try:
        from sentence_transformers import SentenceTransformer
        try:
            app.state.embedding_model = SentenceTransformer('BAAI/bge-small-en-v1.5')
        except Exception:
            app.state.embedding_model = SentenceTransformer('bge-small-en-v1.5')
        logger.info("Lifespan: SentenceTransformer('bge-small-en-v1.5') pre-loaded into app.state.embedding_model")
    except Exception as e:
        logger.error(f"Lifespan: Failed to pre-load SentenceTransformer: {e}")
        app.state.embedding_model = None

    # Connect to async Redis
    global async_redis_client
    try:
        async_redis_client = aioredis.from_url(
            "redis://localhost:6379",
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0
        )
        await asyncio.wait_for(async_redis_client.ping(), timeout=1.0)
        app.state.redis = async_redis_client
        logger.info("Lifespan: Connected to async Redis at redis://localhost:6379")
    except Exception as e:
        logger.warning(f"Lifespan: Redis unavailable ({e}), falling back seamlessly to RAM cache (cachetools)")
        app.state.redis = None
        async_redis_client = None

    yield

    # Shutdown
    logger.info("Lifespan: Shutting down services...")
    try:
        zepto_pool.close()
    except Exception:
        pass
    if async_redis_client is not None:
        try:
            await async_redis_client.aclose()
        except Exception:
            pass
    if redis_client is not None:
        try:
            redis_client.close()
        except Exception:
            pass


app = FastAPI(
    title="Live Quick Commerce Price Aggregator",
    version="4.0.0",
    lifespan=lifespan
)

class SearchStartRequest(BaseModel):
    query: str
    latitude: float
    longitude: float
    previous_job_id: Optional[str] = None


@app.post("/api/search/start")
async def start_search_job(payload: SearchStartRequest):
    """
    Start a background search.

    Returns immediately. Blinkit and Zepto run concurrently in the background.
    """
    cleaned_q = (payload.query or "").strip()

    if not cleaned_q:
        raise HTTPException(status_code=400, detail="Search query is required.")

    try:
        lat = float(payload.latitude)
        lon = float(payload.longitude)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid location coordinates.")

    _cleanup_search_jobs()

    job_id = _new_search_job(
        query=cleaned_q,
        latitude=lat,
        longitude=lon,
        previous_job_id=payload.previous_job_id,
    )

    # Start the job only after its state has been inserted into SEARCH_JOBS.
    task = asyncio.create_task(
        _run_search_job(job_id),
        name=f"quickcompare-{job_id}",
    )
    SEARCH_JOBS[job_id]["manager_task"] = task

    logger.info(
        "[SEARCH JOB %s] created | query=%r | lat=%s | lon=%s",
        job_id[:8],
        cleaned_q,
        lat,
        lon,
    )

    return {
        "job_id": job_id,
        "status": "started",
        "query": cleaned_q,
        "batch_size": SEARCH_JOB_BATCH_SIZE,
    }


@app.get("/api/search/{job_id}/status")
async def get_search_job_status(job_id: str):
    """Poll the lifecycle/readiness of a background search."""
    job = _get_search_job(job_id)
    return _search_job_public_status(job)


@app.get("/api/search/{job_id}/batch/{provider}/{batch_number}")
async def get_search_job_batch(
    job_id: str,
    provider: str,
    batch_number: int,
):
    """
    Return one provider batch.

    batch_number is zero-based.
    Maximum response size is SEARCH_JOB_BATCH_SIZE (50).
    """
    job = _get_search_job(job_id)

    provider = provider.strip().lower()
    if provider not in {"blinkit", "zepto", "instamart"}:
        raise HTTPException(
            status_code=400,
            detail="Provider must be 'blinkit', 'zepto', or 'instamart'.",
        )

    if batch_number < 0:
        raise HTTPException(
            status_code=400,
            detail="batch_number must be >= 0.",
        )

    state = job[provider]
    state_status = state.get("status", "pending")

    # A provider can be actively collecting while already exposing usable
    # products. Blinkit uses this state after its first fast response.
    if state_status in {"pending", "running"}:
        items = state.get("items") or []
        start = batch_number * SEARCH_JOB_BATCH_SIZE
        end = start + SEARCH_JOB_BATCH_SIZE
        batch = items[start:end]
        return {
            "job_id": job_id,
            "provider": provider,
            "batch_number": batch_number,
            "batch_size": SEARCH_JOB_BATCH_SIZE,
            "status": state_status,
            "ready": bool(batch),
            "processing": state_status == "running",
            "items": batch,
            "count": len(items),
            "returned": len(batch),
            "has_more": True if end < len(items) else state_status == "running",
            "next_batch": batch_number + 1 if end < len(items) else None,
        }

    if state_status == "cancelled":
        return {
            "job_id": job_id,
            "provider": provider,
            "batch_number": batch_number,
            "batch_size": SEARCH_JOB_BATCH_SIZE,
            "status": "cancelled",
            "ready": False,
            "processing": False,
            "items": [],
            "count": len(state.get("items") or []),
            "has_more": False,
        }

    if state_status == "failed":
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"{provider.title()} provider failed.",
                "error": state.get("error"),
            },
        )

    items = state.get("items") or []
    start = batch_number * SEARCH_JOB_BATCH_SIZE
    end = start + SEARCH_JOB_BATCH_SIZE
    batch = items[start:end]

    return {
        "job_id": job_id,
        "provider": provider,
        "batch_number": batch_number,
        "batch_size": SEARCH_JOB_BATCH_SIZE,
        "status": "completed",
        "ready": True,
        "processing": False,
        "items": batch,
        "count": len(items),
        "returned": len(batch),
        "has_more": end < len(items),
        "next_batch": batch_number + 1 if end < len(items) else None,
    }


@app.get("/api/search/{job_id}/compare/{batch_number}")
async def get_search_job_comparison_batch(job_id: str, batch_number: int):
    """
    Return one PREPARED QuickCompare comparison batch.

    Non-final batches are always exactly 50 cards. A partial batch is never
    returned while the providers are still collecting. The final batch may
    contain fewer than 50 cards.
    """
    job = _get_search_job(job_id)

    if batch_number < 0:
        raise HTTPException(
            status_code=400,
            detail="batch_number must be >= 0.",
        )

    deadline = (
        asyncio.get_running_loop().time()
        + SEARCH_JOB_COMPARISON_WAIT_SECONDS
    )

    while True:
        job["last_access"] = time.time()

        comparison_batches = job.get("comparison_batches") or []

        # The requested batch has already been completely prepared.
        if batch_number < len(comparison_batches):
            batch = comparison_batches[batch_number]
            final = (
                job.get("comparison_finalized", False)
                and batch_number == len(comparison_batches) - 1
            )

            return {
                "job_id": job_id,
                "batch_number": batch_number,
                "batch_size": SEARCH_JOB_BATCH_SIZE,
                "status": job.get("status", "running"),
                "processing": not job.get("comparison_finalized", False),
                "ready": True,
                "canonical_cards": batch,
                "count": len(batch),
                "returned": len(batch),
                "has_more": (
                    batch_number + 1 < len(comparison_batches)
                    or not job.get("comparison_finalized", False)
                ),
                "next_batch": (
                    batch_number + 1
                    if (
                        batch_number + 1 < len(comparison_batches)
                        or not job.get("comparison_finalized", False)
                    )
                    else None
                ),
                "final": final,
                "prepared_batches": len(comparison_batches),
                "raw_blinkit_count": len(job["blinkit"].get("items") or []),
                "raw_zepto_count": len(job["zepto"].get("items") or []),
                "raw_instamart_count": len(job["instamart"].get("items") or []),
            }

        # The requested batch is not ready. Never return whatever partial
        # canonical_cards happen to exist. Wait briefly for the background
        # comparison builder instead.
        if asyncio.get_running_loop().time() >= deadline:
            return {
                "job_id": job_id,
                "batch_number": batch_number,
                "batch_size": SEARCH_JOB_BATCH_SIZE,
                "status": job.get("status", "running"),
                "processing": not job.get("comparison_finalized", False),
                "ready": False,
                "canonical_cards": [],
                "count": len(job.get("comparison_cards") or []),
                "returned": 0,
                "has_more": not job.get("comparison_finalized", False),
                "next_batch": batch_number,
                "final": False,
                "prepared_batches": len(comparison_batches),
                "retry_after_ms": 300,
                "raw_blinkit_count": len(job["blinkit"].get("items") or []),
                "raw_zepto_count": len(job["zepto"].get("items") or []),
                "raw_instamart_count": len(job["instamart"].get("items") or []),
            }

        await asyncio.sleep(SEARCH_JOB_COMPARISON_POLL_SECONDS)


@app.post("/api/search/{job_id}/cancel")
async def cancel_search_job(job_id: str):
    """Cancel a running search job and its provider tasks."""
    job = _get_search_job(job_id)

    if job.get("status") in {"completed", "failed", "cancelled"}:
        return {
            "job_id": job_id,
            "status": job.get("status"),
            "cancelled": job.get("status") == "cancelled",
        }

    job["cancel_requested"] = True
    job["status"] = "cancelled"

    for task in list(job.get("tasks", {}).values()):
        if task and not task.done():
            task.cancel()

    comparison_task = job.get("comparison_prepare_task")
    if comparison_task and not comparison_task.done():
        comparison_task.cancel()

    manager_task = job.get("manager_task")
    if manager_task and not manager_task.done():
        # Do not cancel the manager task itself here; it will observe the
        # provider cancellations and finish its gather cleanly.
        pass

    for provider_name in ("blinkit", "zepto", "instamart"):
        if job[provider_name].get("status") in {"pending", "running"}:
            job[provider_name]["status"] = "cancelled"

    logger.info("[SEARCH JOB %s] cancelled by API", job_id[:8])

    return {
        "job_id": job_id,
        "status": "cancelled",
        "cancelled": True,
    }



def get_image_hash_cached(image_url: str) -> DualTierCacheStr:
    """
    Dual-tier caching for image hashes:
    Check cachetools.LRUCache (RAM) -> Check Redis -> Compute miss -> Set in both Redis (7-day TTL) and RAM.
    Handles connection timeouts by falling back seamlessly to RAM caching.
    """
    if not image_url:
        async def _empty():
            return ""
        return DualTierCacheStr("", _empty)

    redis_key = f"imghash:{image_url}"
    ram_alt_key = f"img_hash:{image_url}"

    def _compute_hash() -> str:
        return hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:16]

    # Synchronous tier evaluation:
    sync_val = None
    if redis_key in RAM_IMAGE_HASH_CACHE:
        sync_val = RAM_IMAGE_HASH_CACHE[redis_key]
    elif ram_alt_key in RAM_IMAGE_HASH_CACHE:
        sync_val = RAM_IMAGE_HASH_CACHE[ram_alt_key]

    if not sync_val:
        sr = get_sync_redis_client()
        if sr:
            try:
                val = sr.get(redis_key)
                if val:
                    sync_val = val
            except Exception:
                pass
        if not sync_val:
            sync_val = _compute_hash()
            if sr:
                try:
                    sr.setex(redis_key, IMAGE_HASH_TTL, sync_val)
                except Exception:
                    pass
        RAM_IMAGE_HASH_CACHE[redis_key] = sync_val
        RAM_IMAGE_HASH_CACHE[ram_alt_key] = sync_val

    # Asynchronous dual-tier coroutine:
    async def _async_hash() -> str:
        # 1. Check RAM cache first
        if redis_key in RAM_IMAGE_HASH_CACHE:
            return RAM_IMAGE_HASH_CACHE[redis_key]
        if ram_alt_key in RAM_IMAGE_HASH_CACHE:
            return RAM_IMAGE_HASH_CACHE[ram_alt_key]

        # 2. Check async Redis
        global async_redis_client
        if async_redis_client is not None:
            try:
                val = await asyncio.wait_for(async_redis_client.get(redis_key), timeout=1.0)
                if val:
                    RAM_IMAGE_HASH_CACHE[redis_key] = val
                    RAM_IMAGE_HASH_CACHE[ram_alt_key] = val
                    return val
            except Exception as e:
                logger.debug(f"Redis get timeout/error for {redis_key}: {e}")

        # 3. Compute miss
        hash_val = _compute_hash()

        # 4. Set in both Redis (7-day TTL) and RAM
        RAM_IMAGE_HASH_CACHE[redis_key] = hash_val
        RAM_IMAGE_HASH_CACHE[ram_alt_key] = hash_val
        if async_redis_client is not None:
            try:
                await asyncio.wait_for(async_redis_client.setex(redis_key, IMAGE_HASH_TTL, hash_val), timeout=1.0)
            except Exception as e:
                logger.debug(f"Redis setex timeout/error for {redis_key}: {e}")

        return hash_val

    return DualTierCacheStr(sync_val, _async_hash)


class PriceRequest(BaseModel):
    latitude: float
    longitude: float
    query: str

# Setup CORS allowing requests from any local subnet origin, LAN devices, and localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3})(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global persistent session for Instamart
im_session = requests.Session(impersonate="chrome124")


def _clean_str(val: Any) -> Optional[str]:
    if isinstance(val, (str, int, float)):
        s = str(val).strip()
        if s:
            return s
    return None


def extract_quantity_in_base_units(text: str):
    """Returns (amount, 'g' or 'ml') converted to base grams or milliliters."""
    if not text:
        return 1.0, 'unit'
    t = text.lower()
    # Check liters
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:l|ltr|litre|litres)\b', t)
    if m:
        return float(m.group(1)) * 1000.0, 'ml'
    # Check ml
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:ml|milli)\b', t)
    if m:
        return float(m.group(1)), 'ml'
    # Check kg
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:kg|kilo)\b', t)
    if m:
        return float(m.group(1)) * 1000.0, 'g'
    # Check g
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:g|gm|grams)\b', t)
    if m:
        return float(m.group(1)), 'g'
    return 1.0, 'unit'


def extract_packsize_from_title(title: str, current_pack: str = "") -> str:
    current = (current_pack or "").strip()
    c_lower = current.lower()
    if not current or c_lower in ["1 pack", "1 pc", "1 piece", "pack", "unit", "1 unit", ""]:
        m = re.search(r'\b(\d+(?:\.\d+)?)\s*(ml|l|ltr|litre|litres|liter|kg|kilo|g|gm|grams?)\b', title, re.IGNORECASE)
        if m:
            val = m.group(1)
            u = m.group(2).lower()
            if u in ['l', 'ltr', 'litre', 'litres', 'liter']:
                return f"{val} L"
            elif u in ['ml']:
                return f"{val} ml"
            elif u in ['kg', 'kilo']:
                return f"{val} kg"
            elif u in ['g', 'gm', 'gram', 'grams']:
                return f"{val} g"
            return f"{val} {u}"
    return current or "1 pack"


def calculate_unit_price(name: str, packsize: str, price: float) -> Dict[str, Any]:
    if price is None or price <= 0:
        return {"normalized_price": 0.0, "unit": "per pack", "formatted": "₹0.00 / pack"}

    combined = f"{name} {packsize}".lower()

    mult_match = re.search(
        r'(\d+)\s*[xX*]\s*(\d+(?:\.\d+)?)\s*(kg|kilo|kilogram|g|gm|gms|gram|l|ltr|litre|liter|ml)\b',
        combined
    )
    if mult_match:
        multiplier = float(mult_match.group(1))
        sub_qty, base_u = extract_quantity_in_base_units(f"{mult_match.group(2)} {mult_match.group(3)}")
        total_base = multiplier * sub_qty
        if base_u == 'g':
            norm = round((price / total_base) * 100, 2)
            return {"normalized_price": norm, "unit": "100g", "formatted": f"₹{norm:.2f} / 100g"}
        elif base_u == 'ml':
            norm = round((price / total_base) * 100, 2)
            return {"normalized_price": norm, "unit": "100ml", "formatted": f"₹{norm:.2f} / 100ml"}

    base_qty, base_u = extract_quantity_in_base_units(combined)
    if base_u == 'g' and base_qty > 0:
        norm = round((price / base_qty) * 100, 2)
        return {"normalized_price": norm, "unit": "100g", "formatted": f"₹{norm:.2f} / 100g"}
    elif base_u == 'ml' and base_qty > 0:
        norm = round((price / base_qty) * 100, 2)
        return {"normalized_price": norm, "unit": "100ml", "formatted": f"₹{norm:.2f} / 100ml"}

    return {
        "normalized_price": round(price, 2),
        "unit": "per pack",
        "formatted": f"₹{price:.2f} / pack"
    }


def extract_volume_units(name: str, packsize: str) -> Optional[float]:
    combined = f"{name} {packsize}".lower()
    mult_match = re.search(
        r'(\d+)\s*[xX*]\s*(\d+(?:\.\d+)?)\s*(kg|kilo|kilogram|g|gm|gms|gram|l|ltr|litre|liter|ml)\b',
        combined
    )
    if mult_match:
        multiplier = float(mult_match.group(1))
        sub_qty, base_u = extract_quantity_in_base_units(f"{mult_match.group(2)} {mult_match.group(3)}")
        if base_u in ('g', 'ml'):
            return multiplier * sub_qty

    base_qty, base_u = extract_quantity_in_base_units(combined)
    if base_u in ('g', 'ml'):
        return base_qty

    return None


def _get_blinkit_auth_key() -> str:
    """
    Obtains a temporary anonymous auth_key from Blinkit accounts API to authorize search requests.
    """
    try:
        r = requests.post(
            "https://blinkit.com/v2/accounts/auth_key/",
            headers={
                "app_client": "consumer_web",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            },
            impersonate="chrome124",
            timeout=5
        )
        if r.status_code == 200:
            return r.json().get("auth_key", "")
    except Exception as e:
        logger.warning(f"Failed to obtain Blinkit auth_key: {e}")
    return ""


# Location-First Dark Store Mapping:
DARK_STORE_MAP = [
    # Mumbai
    {"lat": 19.0760, "lon": 72.8777, "radius": 0.5, "store_id": "e9b25f0e-d7f4-42b7-a3f2-89b9d31317d7", "city": "Mumbai"},
    # Bangalore
    {"lat": 12.9716, "lon": 77.5946, "radius": 0.5, "store_id": "f5d023f9-7151-40be-bd38-9ef6a836d538", "city": "Bangalore"},
    # Delhi NCR
    {"lat": 28.6139, "lon": 77.2090, "radius": 0.5, "store_id": "7fa12bf2-2b63-4416-8de7-200a74794e33", "city": "Delhi"},
]

def resolve_dark_store_id(lat: float, lon: float) -> DualTierCacheStr:
    """
    Location-First Dark Store Mapping with Dual-Tier Caching:
    Check cachetools.LRUCache (RAM) -> Check Redis -> Compute miss -> Set in both Redis (24-hour TTL) and RAM.
    Handles connection timeouts by falling back seamlessly to RAM caching.
    """
    try:
        f_lat = round(float(lat), 4)
        f_lon = round(float(lon), 4)
    except Exception:
        fallback_id = "e9b25f0e-d7f4-42b7-a3f2-89b9d31317d7"
        async def _fb():
            return fallback_id
        return DualTierCacheStr(fallback_id, _fb)

    redis_key = f"darkstore:{f_lat}:{f_lon}"
    ram_alt_key = f"dark_store:{f_lat}:{f_lon}"

    def _compute_miss() -> str:
        for entry in DARK_STORE_MAP:
            if abs(f_lat - entry["lat"]) <= entry["radius"] and abs(f_lon - entry["lon"]) <= entry["radius"]:
                return entry["store_id"]
        return "e9b25f0e-d7f4-42b7-a3f2-89b9d31317d7"

    # Synchronous tier evaluation:
    sync_val = None
    if redis_key in RAM_DARK_STORE_CACHE:
        sync_val = RAM_DARK_STORE_CACHE[redis_key]
    elif ram_alt_key in RAM_DARK_STORE_CACHE:
        sync_val = RAM_DARK_STORE_CACHE[ram_alt_key]

    if not sync_val:
        sr = get_sync_redis_client()
        if sr:
            try:
                val = sr.get(redis_key)
                if val:
                    sync_val = val
            except Exception:
                pass
        if not sync_val:
            sync_val = _compute_miss()
            if sr:
                try:
                    sr.setex(redis_key, DARK_STORE_TTL, sync_val)
                except Exception:
                    pass
        RAM_DARK_STORE_CACHE[redis_key] = sync_val
        RAM_DARK_STORE_CACHE[ram_alt_key] = sync_val

    # Asynchronous dual-tier coroutine:
    async def _async_resolve() -> str:
        # 1. Check RAM cache first
        if redis_key in RAM_DARK_STORE_CACHE:
            return RAM_DARK_STORE_CACHE[redis_key]
        if ram_alt_key in RAM_DARK_STORE_CACHE:
            return RAM_DARK_STORE_CACHE[ram_alt_key]

        # 2. Check async Redis
        global async_redis_client
        if async_redis_client is not None:
            try:
                val = await asyncio.wait_for(async_redis_client.get(redis_key), timeout=1.0)
                if val:
                    RAM_DARK_STORE_CACHE[redis_key] = val
                    RAM_DARK_STORE_CACHE[ram_alt_key] = val
                    return val
            except Exception as e:
                logger.debug(f"Redis get timeout/error for {redis_key}: {e}")

        # 3. Compute miss: Dynamic handshake via resolve_zepto_store_id
        try:
            resolved = await resolve_zepto_store_id(f_lat, f_lon)
        except Exception as e:
            logger.debug(f"resolve_zepto_store_id fallback: {e}")
            resolved = _compute_miss()

        # 4. Set in both Redis (24-hour TTL) and RAM
        RAM_DARK_STORE_CACHE[redis_key] = resolved
        RAM_DARK_STORE_CACHE[ram_alt_key] = resolved
        if async_redis_client is not None:
            try:
                await asyncio.wait_for(async_redis_client.setex(redis_key, DARK_STORE_TTL, resolved), timeout=1.0)
            except Exception as e:
                logger.debug(f"Redis setex timeout/error for {redis_key}: {e}")

        return resolved

    return DualTierCacheStr(sync_val, _async_resolve)


fetch_blinkit = fetch_blinkit_products
fetch_zepto_uc = fetch_zepto_products


def fetch_blinkit_sync(query: str, lat: float, lon: float, store_id: str = None) -> List[Dict[str, Any]]:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, fetch_blinkit_products(query, lat, lon, store_id)).result()
        return loop.run_until_complete(fetch_blinkit_products(query, lat, lon, store_id))
    except Exception:
        return asyncio.run(fetch_blinkit_products(query, lat, lon, store_id))


def fetch_zepto_uc_sync(query: str, lat: float, lon: float, store_id: str = "") -> List[Dict[str, Any]]:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, fetch_zepto_products(query, lat, lon, store_id)).result()
        return loop.run_until_complete(fetch_zepto_products(query, lat, lon, store_id))
    except Exception:
        return asyncio.run(fetch_zepto_products(query, lat, lon, store_id))


def fetch_zepto_sync(
    query: str,
    lat: str,
    lon: str,
    x_zepto_cookie: Optional[str] = None
) -> tuple[List[Dict[str, Any]], bool]:
    items = fetch_zepto_uc_sync(query, float(lat), float(lon))
    return items, False


async def fetch_zepto(
    query: str,
    lat: str,
    lon: str,
    x_zepto_cookie: Optional[str] = None
) -> tuple[List[Dict[str, Any]], bool]:
    items = await fetch_zepto_products(query, float(lat), float(lon), client_cookie=x_zepto_cookie)
    return items, False


def _parse_instamart_item(info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = info.get("name", "").strip()
    if not name or len(name) < 2:
        return None

    # Inspect finalPrice, price, and defaultPrice (prefer finalPrice to capture active promotional discounts)
    raw_price = info.get("finalPrice") or info.get("price") or info.get("defaultPrice") or 0
    try:
        raw_val = float(raw_price)
    except (ValueError, TypeError):
        return None
    if raw_val <= 0:
        return None

    # Normalize: Swiggy API returns prices in paise (> 1000 for regular items)
    price = raw_val / 100.0 if raw_val > 500 else raw_val
    if price < 10 or price > 15000:
        return None

    # Extract MRP if available
    raw_mrp = info.get("mrp") or info.get("originalPrice") or 0
    try:
        mrp_val = float(raw_mrp)
        mrp = mrp_val / 100.0 if mrp_val > 500 else mrp_val
    except (ValueError, TypeError):
        mrp = price

    # Clean out packaging descriptions so packsize reflects items like "500 ml", "1 L", or "400 g"
    raw_pack = str(info.get("quantity") or info.get("variationTitle") or info.get("variation_title") or info.get("packsize") or "").strip()
    pack_match = re.search(r'\b(\d+(?:\.\d+)?\s*(?:ml|l|ltr|litre|liter|g|gm|gms|gram|kg|pc|pack|piece)s?)\b', f"{name} {raw_pack}", re.IGNORECASE)
    packsize = pack_match.group(1) if pack_match else (raw_pack if raw_pack else "1 pack")

    img_id = info.get("imageId", "") or info.get("image", {}).get("id", "")
    img_url = f"https://media-assets.swiggy.com/swiggy/image/upload/fl_lossy,f_auto,q_auto,w_252,h_252/{img_id}" if img_id else ""
    buy_url = f"https://www.swiggy.com/instamart/search?query={urllib.parse.quote(name)}"

    return {
        "name": name,
        "price": round(price, 2),
        "mrp": round(mrp, 2) if mrp >= price else round(price, 2),
        "packsize": packsize,
        "image_url": img_url,
        "buy_url": buy_url
    }


@app.get("/api/instamart")
def get_instamart_products(
    q: str = Query(..., description="Query"),
    lat: str = Query(...),
    lon: str = Query(...)
):
    encoded_q = urllib.parse.quote(q)
    try:
        if not im_session.cookies:
            im_session.get(
                f"https://www.swiggy.com/instamart?lat={lat}&lng={lon}",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
                timeout=5
            )
    except Exception:
        pass

    endpoints = [
        f"https://www.swiggy.com/dapi/landing/search?query={encoded_q}&lat={lat}&lng={lon}",
        f"https://www.swiggy.com/dapi/restaurants/search/v3?lat={lat}&lng={lon}&str={encoded_q}&trackingId=undefined&submitAction=ENTER&queryUniqueId=quickcompare_im&selectedPLTab=INSTAMART"
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.swiggy.com/instamart",
        "Origin": "https://www.swiggy.com",
        "device-type": "web"
    }

    products = []
    for url in endpoints:
        try:
            res = im_session.get(url, headers=headers, timeout=8)
            if res.status_code != 200:
                continue
            data = res.json().get("data", {})
            
            def extract_items(node):
                if isinstance(node, dict):
                    if "name" in node and ("price" in node or "finalPrice" in node or "defaultPrice" in node):
                        # Reject cooked dishes
                        if "restaurantId" not in node and not ("itemAttribute" in node and "vegClassifier" in str(node)):
                            raw_price = node.get("finalPrice") or node.get("price") or node.get("defaultPrice") or 0
                            price = float(raw_price) / 100.0 if raw_price > 500 else float(raw_price)
                            pack = node.get("quantity") or node.get("variationTitle") or "1 pack"
                            img_id = node.get("imageId", "")
                            if price >= 10:
                                products.append({
                                    "name": node.get("name", ""),
                                    "price": round(price, 2),
                                    "packsize": pack,
                                    "image_url": f"https://media-assets.swiggy.com/swiggy/image/upload/fl_lossy,f_auto,q_auto,w_252,h_252/{img_id}" if img_id else "",
                                    "buy_url": f"https://www.swiggy.com/instamart/search?query={encoded_q}"
                                })
                    for v in node.values():
                        extract_items(v)
                elif isinstance(node, list):
                    for i in node:
                        extract_items(i)

            extract_items(data)
            if products:
                break
        except Exception:
            continue

    seen = set()
    deduped = []
    for p in products:
        if p["name"] not in seen:
            seen.add(p["name"])
            deduped.append(p)
    return {"items": deduped[:15]}


def fetch_instamart_sync(query: str, lat: str, lon: str) -> List[Dict[str, Any]]:
    return get_instamart_products(query, lat, lon).get("items", [])


async def fetch_instamart(query: str, lat: str, lon: str) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(fetch_instamart_sync, query, lat, lon)


@app.get("/api/blinkit")
async def get_blinkit_products(
    q: str = Query(..., description="Query"),
    lat: Optional[str] = Query(None),
    lon: Optional[str] = Query(None),
    latitude: Optional[str] = Query(None),
    longitude: Optional[str] = Query(None),
    store_id: Optional[str] = Query(None),
    x_store_id: Optional[str] = Header(None, alias="x-store-id"),
    x_location_lat: Optional[str] = Header(None, alias="X-Location-Lat"),
    x_location_lng: Optional[str] = Header(None, alias="X-Location-Lng"),
    h_lat: Optional[str] = Header(None, alias="latitude"),
    h_lon: Optional[str] = Header(None, alias="longitude"),
    h_lat_short: Optional[str] = Header(None, alias="lat"),
    h_lng_short: Optional[str] = Header(None, alias="lng")
):
    cleaned_q = q.strip()
    if not cleaned_q:
        return {"items": [], "count": 0}
    resolved_lat = _clean_str(lat) or _clean_str(latitude) or _clean_str(x_location_lat) or _clean_str(h_lat) or _clean_str(h_lat_short)
    resolved_lon = _clean_str(lon) or _clean_str(longitude) or _clean_str(x_location_lng) or _clean_str(h_lon) or _clean_str(h_lng_short)
    resolved_store_id = _clean_str(store_id) or _clean_str(x_store_id)
    if not resolved_lat or not resolved_lon:
        return {"items": [], "count": 0, "error": "Location coordinates required"}
    items = await fetch_blinkit(cleaned_q, resolved_lat, resolved_lon, resolved_store_id)
    return {"items": items, "count": len(items)}


KNOWN_BRANDS = [
    "mother dairy", "country delight", "tata sampann", "tata tea", "tata salt", "tata",
    "epigamia", "amul", "nandini", "nestle", "britannia", "aashirvaad", "fortune",
    "saffola", "parle", "cadbury", "coca-cola", "pepsi", "thums up", "sprite",
    "lays", "kurkure", "haldiram", "bingo", "dettol", "dove", "colgate",
    "surf excel", "ariel", "vim", "whisper", "horlicks", "bournvita",
    "heritage", "milky mist", "gowardhan", "maggi", "kissan", "del monte",
    "kelloggs", "quaker", "real", "tropicana", "paper boat", "bisleri", "dabur"
]

KNOWN_TYPES = [
    "greek yogurt", "greek yoghurt", "yogurt", "yoghurt", "curd", "dahi", "buttermilk", "chaas",
    "cheese slices", "cheese cube", "cheese cubes", "cheese spread", "cheese",
    "butter", "ghee", "milk", "cream", "malai", "milk powder", "dairy whitener",
    "atta", "maida", "sooji", "suji", "rawa", "besan", "rice", "poha", "oats", "muesli", "corn flakes",
    "mustard oil", "sunflower oil", "refined oil", "groundnut oil", "olive oil", "soybean oil", "oil",
    "salt", "sugar", "jaggery", "gur", "green tea", "tea", "coffee",
    "biscuits", "cookies", "rusk", "brown bread", "white bread", "bread", "pav", "buns",
    "noodles", "pasta", "macaroni", "ketchup", "sauce", "mayonnaise", "jam",
    "chips", "namkeen", "bhujia", "peanuts", "chocolate",
    "soap", "body wash", "face wash", "shampoo", "conditioner", "toothpaste"
]

KNOWN_VARIANTS = [
    # Flavors
    "blueberry", "strawberry", "mango", "alphonso", "vanilla", "chocolate", "mixed fruit",
    "pineapple", "banana", "green apple", "apple", "orange", "litchi", "guava", "watermelon",
    "peach", "cranberry", "raspberry", "pomegranate", "fig", "anjeer", "honey", "salted caramel",
    # Savory
    "magic masala", "masala", "jeera", "garlic", "ginger", "green chilli", "chilli",
    "peri peri", "sour cream & onion", "cream & onion", "tangy tomato", "tomato",
    "plain salted", "salted", "unsalted", "pepper",
    # Milk & Dairy Fat / Grades
    "full cream", "double toned", "toned", "standardized", "skimmed", "slim", "cow", "buffalo",
    "homogenized", "pasteurized", "tetra", "organic", "a2",
    # General
    "masala chai", "elaichi", "cardamom", "gold", "premium", "classic", "natural", "original", "everyday"
]


# 1. Strict Category & Keyword Guardrails
INCOMPATIBLE_KEYWORD_PAIRS = [
    ({"powder"}, {"spread", "fat", "liquid", "curd", "yogurt", "cheese", "butter", "cream"}),
    ({"spread"}, {"powder", "slice", "slices", "cube", "cubes", "butter", "milk"}),
    ({"butter"}, {"powder", "spread", "cheese", "curd", "paneer", "oil", "ghee", "milk"}),
    ({"cheese"}, {"butter", "curd", "paneer", "yogurt", "milk", "powder"}),
    ({"bread"}, {"biscuit", "biscuits", "rusk", "cookie", "cookies", "cake", "buns", "pav"}),
    ({"curd", "dahi"}, {"milk", "paneer", "cheese", "butter", "powder"}),
    ({"paneer"}, {"curd", "dahi", "cheese", "butter", "milk", "powder"}),
    ({"ghee"}, {"oil", "butter", "milk", "powder"}),
]

KEY_PRODUCT_TYPE_TOKENS = ["powder", "spread", "butter", "bread", "cheese", "curd", "dahi", "paneer", "ghee"]

PACKAGING_TOKENS = ["tetra", "tetrapack", "pouch", "bottle", "can", "tin", "pet bottle", "sachet", "bucket", "tub", "jar"]


def extract_packaging(title: str, packsize: str = "") -> str:
    """
    Extracts packaging tokens (tetra, tetrapack, pouch, bottle, can, tin, pet bottle, sachet, bucket, tub, jar)
    from titles and packsize strings.
    """
    text = f" {title} {packsize} ".lower()
    # Check multi-word tokens first
    if "tetra pack" in text or "tetrapack" in text:
        return "tetra"
    if "pet bottle" in text:
        return "bottle"

    tokens = set(re.findall(r'\b[a-z]+\b', text))
    if "tetra" in tokens:
        return "tetra"
    if "pouch" in tokens or "pouches" in tokens:
        return "pouch"
    if "bottle" in tokens or "bottles" in tokens:
        return "bottle"
    if "can" in tokens or "cans" in tokens:
        return "can"
    if "tin" in tokens or "tins" in tokens:
        return "tin"
    if "sachet" in tokens or "sachets" in tokens:
        return "sachet"
    if "bucket" in tokens or "buckets" in tokens:
        return "bucket"
    if "tub" in tokens or "tubs" in tokens:
        return "tub"
    if "jar" in tokens or "jars" in tokens:
        return "jar"
    return ""

extract_packaging_type = extract_packaging


def check_packaging_guardrail(title_a: str, title_b: str, pack_a: str = "", pack_b: str = "") -> bool:
    """
    Rule: If one product title contains tetra or tetrapack and the other contains pouch (or lacks tetra),
    do not match them, even if brand, name, and weight match.
    Also, if both items specify different explicit packaging types (e.g. tin vs can, pouch vs bottle), do not match.
    """
    pkg_a = extract_packaging(title_a, pack_a)
    pkg_b = extract_packaging(title_b, pack_b)

    # Packaging is a constraint only when BOTH sides explicitly specify it.
    # If one side omits packaging metadata, do not reject an otherwise valid product match.
    if pkg_a and pkg_b and pkg_a != pkg_b:
        return False

    return True


def check_keyword_guardrails(title_a: str, title_b: str) -> bool:
    """
    Returns False if matching should be explicitly aborted due to keyword guardrail violations.
    e.g. powder vs spread/fat, mismatch on key product type tokens, or packaging mismatches (tetra vs pouch/non-tetra).
    """
    # 1. Packaging Guardrail Check
    if not check_packaging_guardrail(title_a, title_b):
        return False

    ta = (title_a or "").lower()
    tb = (title_b or "").lower()
    tokens_a = set(re.findall(r'\b[a-z]+\b', ta))
    tokens_b = set(re.findall(r'\b[a-z]+\b', tb))

    # Incompatible pairs check: e.g. powder vs (spread, fat)
    for group1, group2 in INCOMPATIBLE_KEYWORD_PAIRS:
        if (tokens_a & group1) and (tokens_b & group2):
            return False
        if (tokens_b & group1) and (tokens_a & group2):
            return False

    # Key product type tokens check: if one title has powder/spread/butter/bread/cheese and the other doesn't
    for kt in KEY_PRODUCT_TYPE_TOKENS:
        has_a = kt in tokens_a
        has_b = kt in tokens_b
        if has_a != has_b:
            return False

    return True


def normalize_title_for_matching(title: str) -> str:
    """
    Sanitizes item titles by stripping unnecessary filler words:
    Fresh, Pouch, Everyday, Pack, Net quantity, and extra special characters (|, -).
    Sorts remaining title tokens alphabetically before measuring similarity or grouping into cards.
    """
    if not title:
        return ""
    t = title.lower()
    # Remove packaging / net quantity phrases
    t = re.sub(r'\bnet\s+quantity\b', ' ', t)
    t = re.sub(r'\bnet\s+wt\.?\b', ' ', t)
    t = re.sub(r'\bnet\s+weight\b', ' ', t)
    # Strip unnecessary filler words: fresh, pouch, everyday, pack, packet, pcs, pieces, etc.
    t = re.sub(r'\b(fresh|pouch|everyday|pack|packet|pcs?|pieces?|tetra|carton|bottle|tin|can|box|jar)\b', ' ', t)
    # Strip metric units and quantities
    t = re.sub(r'\b\d+(\.\d+)?\s*(ml|milli|l|ltr|litre|litres|liter|g|gm|grams?|kg|kilo)\b', ' ', t)
    # Strip numbers
    t = re.sub(r'\b\d+\b', ' ', t)
    # Replace special characters (|, -, /, etc) with space
    t = re.sub(r'[|\-_/\\(),.:;+*&]', ' ', t)
    # Tokenize, strip, and sort alphabetically
    tokens = [tok for tok in t.split() if len(tok) > 1]
    tokens.sort()
    return " ".join(tokens)


def clean_canonical_key(title: str) -> str:
    clean = title.lower()
    clean = re.sub(r'\b(\d+(\.\d+)?\s*(ml|milli|l|ltr|litre|litres|liter|g|gm|grams?|kg|kilo|pack|pcs?|pieces?|pouch|tetra))\b', '', clean)
    clean = re.sub(r'[^a-z0-9\s]', ' ', clean)
    return " ".join(clean.split())


def extract_normalized_quantity(title: str, packsize: str = "") -> tuple[float, str]:
    """
    Normalizes quantity and base metric unit from title and packsize,
    detecting multipack patterns (e.g. 'Pack of 2', '2 x 500ml', 'Buy 1 Get 1')
    and computing the total normalized volume/weight after multipack expansion.
    """
    combined = f"{packsize} {title}".lower()

    # 1. Check for Buy X Get Y (e.g., 'Buy 1 Get 1', 'Buy 2 Get 1 Free', 'BOGO', 'B1G1')
    bogo_mult = 1.0
    bogo_match = re.search(r'\bbuy\s+(\d+)\s+get\s+(\d+)\b', combined)
    if bogo_match:
        bogo_mult = float(int(bogo_match.group(1)) + int(bogo_match.group(2)))
    elif re.search(r'\b(bogo|b1g1)\b', combined):
        bogo_mult = 2.0

    # 2. Check for Pack/Combo/Set of N (e.g., 'Pack of 2', 'Set of 3', 'Combo of 4')
    pack_of_mult = 1.0
    pack_of_match = re.search(r'\b(?:pack|set|combo|box|case|pk)\s+of\s+(\d+)\b', combined)
    if pack_of_match:
        pack_of_mult = float(pack_of_match.group(1))
    else:
        # e.g., '2 pack', '3 pk', '2 units pack'
        p_match2 = re.search(r'\b(\d+)\s*(?:pack|pk|pcs|pieces|units)\b', combined)
        if p_match2 and not re.search(r'\b1\s*(?:pack|pk|pcs|pieces|units)\b', p_match2.group(0)):
            pack_of_mult = float(p_match2.group(1))

    external_multiplier = bogo_mult * pack_of_mult

    # 3. Check for explicit multipack pattern 'N x Quantity' (e.g., '2 x 500ml', '2x500 ml', '3 * 1 L')
    mult1 = re.search(r'(\d+)\s*[xX*]\s*(\d+(?:\.\d+)?)\s*(l|ltr|litre|litres|liter|ml|kg|kilo|g|gm|grams?)\b', combined)
    if mult1:
        m = float(mult1.group(1))
        sub_val = float(mult1.group(2))
        u = mult1.group(3)
        total_m = m * (bogo_mult if bogo_match or re.search(r'\b(bogo|b1g1)\b', combined) else 1.0)
        if u in ['l', 'ltr', 'litre', 'litres', 'liter']:
            return total_m * sub_val * 1000.0, 'ml'
        elif u in ['ml']:
            return total_m * sub_val, 'ml'
        elif u in ['kg', 'kilo']:
            return total_m * sub_val * 1000.0, 'g'
        elif u in ['g', 'gm', 'gram', 'grams']:
            return total_m * sub_val, 'g'

    # Check reversed explicit multipack 'Quantity x N' (e.g., '500ml x 2', '500g * 3')
    mult2 = re.search(r'(\d+(?:\.\d+)?)\s*(l|ltr|litre|litres|liter|ml|kg|kilo|g|gm|grams?)\s*[xX*]\s*(\d+)\b', combined)
    if mult2:
        sub_val = float(mult2.group(1))
        u = mult2.group(2)
        m = float(mult2.group(3))
        total_m = m * (bogo_mult if bogo_match or re.search(r'\b(bogo|b1g1)\b', combined) else 1.0)
        if u in ['l', 'ltr', 'litre', 'litres', 'liter']:
            return total_m * sub_val * 1000.0, 'ml'
        elif u in ['ml']:
            return total_m * sub_val, 'ml'
        elif u in ['kg', 'kilo']:
            return total_m * sub_val * 1000.0, 'g'
        elif u in ['g', 'gm', 'gram', 'grams']:
            return total_m * sub_val, 'g'

    # 4. Single volume/mass measurement combined with external multiplier (Pack of N or BOGO)
    m_lit = re.search(r'(\d+(?:\.\d+)?)\s*(?:l|ltr|litre|litres|liter)\b', combined)
    if m_lit:
        return float(m_lit.group(1)) * 1000.0 * external_multiplier, 'ml'

    m_ml = re.search(r'(\d+(?:\.\d+)?)\s*(?:ml|milli)\b', combined)
    if m_ml:
        return float(m_ml.group(1)) * external_multiplier, 'ml'

    m_kg = re.search(r'(\d+(?:\.\d+)?)\s*(?:kg|kilo)\b', combined)
    if m_kg:
        return float(m_kg.group(1)) * 1000.0 * external_multiplier, 'g'

    m_g = re.search(r'(\d+(?:\.\d+)?)\s*(?:g|gm|grams?)\b', combined)
    if m_g:
        return float(m_g.group(1)) * external_multiplier, 'g'

    # 5. Fallback if only item count/pieces exist (no weight or volume)
    if external_multiplier > 1.0:
        return external_multiplier, 'unit'

    return 0.0, ''


def to_base_metric(packsize: str, name: str) -> tuple[float, str]:
    """Normalizes quantity from packsize or name to base metric volume/mass with multipack expansion."""
    return extract_normalized_quantity(name, packsize)


def compute_standard_unit_price(price: float, quantity: float, unit: str) -> Dict[str, Any]:
    """
    Computes standard unit prices (₹/100g, ₹/100ml, or ₹/unit).
    """
    if price is None or price <= 0 or quantity <= 0:
        return {
            "price_per_unit": 0.0,
            "unit": "unit",
            "metric": "₹/unit",
            "formatted": "₹0.00 / unit"
        }

    if unit == 'ml':
        norm = round((price / quantity) * 100.0, 2)
        return {
            "price_per_unit": norm,
            "unit": "100ml",
            "metric": "₹/100ml",
            "formatted": f"₹{norm:.2f} / 100ml"
        }
    elif unit == 'g':
        norm = round((price / quantity) * 100.0, 2)
        return {
            "price_per_unit": norm,
            "unit": "100g",
            "metric": "₹/100g",
            "formatted": f"₹{norm:.2f} / 100g"
        }
    else:
        norm = round(price / quantity, 2)
        return {
            "price_per_unit": norm,
            "unit": "unit",
            "metric": "₹/unit",
            "formatted": f"₹{norm:.2f} / unit"
        }


def parse_structured_product(title: str, packsize: str = "", price: float = 0.0) -> Dict[str, Any]:
    """
    Normalized Unit & Packaging Extractor (Comparify Architecture):
    Parses product titles into structured fields: brand, name, packaging (pouch, tetra, bottle, etc.),
    quantity, and unit (ml, g, l, kg).
    Normalizes volume (1 L -> 1000 ml) and computes standard unit_price (price / total_volume).
    """
    text = f"{title} {packsize}".lower()

    # 1. Brand
    brand = ""
    for b in KNOWN_BRANDS:
        pattern = r'\b' + re.escape(b) + r'\b'
        if re.search(pattern, text):
            brand = b
            break
    if not brand:
        tokens = re.sub(r'[^a-z0-9\s]', ' ', text).split()
        brand = tokens[0] if tokens else "generic"

    # 2. Product Type / Core Name
    product_type = ""
    for pt in KNOWN_TYPES:
        pattern = r'\b' + re.escape(pt) + r'\b'
        if re.search(pattern, text):
            product_type = pt
            break
    if not product_type:
        product_type = "general"

    # 3. Packaging Type (tetra, pouch, bottle, can, tin, pet bottle, sachet, bucket, tub, jar)
    packaging = extract_packaging_type(title, packsize)

    # 4. Normalized Quantity & Unit (Volume / Mass / Multipack Expansion)
    quantity, unit = extract_normalized_quantity(title, packsize)

    # 5. Unit Price Computation (price / total_volume & standard unit pricing)
    unit_price = round(price / quantity, 4) if (quantity > 0 and price > 0) else 0.0
    std_price_info = compute_standard_unit_price(price, quantity, unit)

    return {
        "brand": brand,
        "name": title,
        "product_type": product_type,
        "packaging": packaging,
        "quantity": quantity,
        "unit": unit,
        "unit_price": unit_price,
        "standard_unit_price": std_price_info["price_per_unit"],
        "standard_unit": std_price_info["unit"],
        "formatted_unit_price": std_price_info["formatted"],
        "price": price,
        "raw_name": title,
        "packsize": packsize
    }


def extract_product_attributes(name: str, packsize: str = "", price: float = 0.0) -> Dict[str, Any]:
    struct = parse_structured_product(name, packsize, price)
    text = f"{name} {packsize}".lower()
    variant = ""
    for v in KNOWN_VARIANTS:
        pattern = r'\b' + re.escape(v) + r'\b'
        if re.search(pattern, text):
            variant = v
            break
    if not variant:
        variant = "plain"

    return {
        "brand": struct["brand"],
        "product_type": struct["product_type"],
        "variant": variant,
        "packaging": struct["packaging"],
        "packaging_type": struct["packaging"],
        "quantity": struct["quantity"],
        "unit": struct["unit"],
        "unit_price": struct["unit_price"],
        "standard_unit_price": struct["standard_unit_price"],
        "standard_unit": struct["standard_unit"],
        "formatted_unit_price": struct["formatted_unit_price"],
        "weight_val": struct["quantity"],
        "weight_unit": struct["unit"],
        "raw_name": name,
        "packsize": packsize,
        "price": price
    }


def strict_product_match_guardrail(a: Any, b: Any, pack_a: str = "", pack_b: str = "") -> bool:
    """
    3-Gate Hard Guardrails (Comparify Architecture):
    Gate 1: Total Volume & Multipack Expansion Guardrail.
            Rejects mismatches where total volume differs after multipack expansion (e.g. 1000 ml vs 500 ml, 85g vs 75g).
    Gate 2: Category & Incompatible keyword guardrail.
            Rejects incompatible product pairs (e.g. powder vs spread/fat, bread vs biscuits).
    Gate 3: Packaging type guardrail.
            Rejects matches immediately if packaging type differs (e.g. tetra vs pouch/non-tetra, bottle vs can).
    """
    if isinstance(a, dict):
        title_a = a.get("raw_name") or a.get("name") or ""
        p_a = pack_a or a.get("packsize") or ""
        pkg_a = a.get("packaging") or a.get("packaging_type") or extract_packaging(title_a, p_a)
        qty_a = a.get("quantity") if a.get("quantity") is not None else a.get("weight_val")
        unit_a = a.get("unit") or a.get("weight_unit")
    else:
        title_a = str(a or "")
        p_a = pack_a
        pkg_a = extract_packaging(title_a, p_a)
        qty_a, unit_a = None, None

    if isinstance(b, dict):
        title_b = b.get("raw_name") or b.get("name") or ""
        p_b = pack_b or b.get("packsize") or ""
        pkg_b = b.get("packaging") or b.get("packaging_type") or extract_packaging(title_b, p_b)
        qty_b = b.get("quantity") if b.get("quantity") is not None else b.get("weight_val")
        unit_b = b.get("unit") or b.get("weight_unit")
    else:
        title_b = str(b or "")
        p_b = pack_b
        pkg_b = extract_packaging(title_b, p_b)
        qty_b, unit_b = None, None

    # Gate 1: Total Volume & Multipack Expansion Guardrail
    if qty_a is None or not unit_a:
        qty_a, unit_a = extract_normalized_quantity(title_a, p_a)
    if qty_b is None or not unit_b:
        qty_b, unit_b = extract_normalized_quantity(title_b, p_b)

    if unit_a and unit_b:
        if unit_a != unit_b:
            return False
        if max(qty_a, qty_b) > 0:
            rel_diff = abs(qty_a - qty_b) / max(qty_a, qty_b)
            if rel_diff > 0.01:
                return False
    elif (p_a or p_b) and (p_a.strip().lower() != p_b.strip().lower()):
        return False

    # Gate 2: Category & Incompatible keyword guardrail
    if not check_keyword_guardrails(title_a, title_b):
        return False

    # Gate 3: Packaging type guardrail
    # Only reject when BOTH products explicitly specify different packaging.
    # Missing packaging metadata on one side is not evidence of a different SKU.
    if pkg_a and pkg_b and pkg_a != pkg_b:
        return False

    return True


def are_products_exact_match(attr_a: Dict[str, Any], attr_b: Dict[str, Any]) -> bool:
    # 0. 3-Gate Hard Guardrails (Category/Keyword, Packaging, Normalized Quantity)
    if not strict_product_match_guardrail(attr_a, attr_b):
        return False

    # 1. Brand Constraint
    ba = attr_a.get("brand", "")
    bb = attr_b.get("brand", "")
    if ba != bb and not (ba in bb or bb in ba):
        return False

    # 2. Product Type Constraint
    ta = attr_a.get("product_type", "")
    tb = attr_b.get("product_type", "")
    if ta != tb and not (ta in tb or tb in ta):
        return False

    # 3. Universal residual product-name similarity.
    # Do NOT depend on a hardcoded variant/flavor dictionary.
    # match_products() removes brand, normalized quantity and common packaging/filler
    # terms, then compares the remaining product identity tokens.
    # This lets arbitrary descriptors work without adding words such as
    # "gold", "classic", "masala", "garlic", "toned", etc. to a list.
    residual_score = match_products(
        attr_a.get("raw_name", ""),
        attr_b.get("raw_name", ""),
        attr_a.get("packsize", ""),
        attr_b.get("packsize", ""),
    )
    if residual_score is None:
        return False

    return True


def build_canonical_cards(
    blinkit_items: list,
    zepto_items: list,
    blinkit_serviceability: str = "serviceable",
    zepto_serviceability: str = "serviceable",
) -> list:
    b_attrs = [(b, extract_product_attributes(b["name"], b.get("packsize", ""), b.get("price", 0.0))) for b in blinkit_items]
    z_attrs = [(z, extract_product_attributes(z["name"], z.get("packsize", ""), z.get("price", 0.0))) for z in zepto_items]

    matched_z_indices = set()
    card_map: Dict[str, Dict[str, Any]] = {}

    for b, b_attr in b_attrs:
        # Standardize Title Normalization & Family Grouping
        pkg_b = b_attr.get("packaging", "")
        canonical_pkg_b = "_tetra" if pkg_b == "tetra" else (f"_{pkg_b}" if pkg_b else "")
        qty_b = b_attr.get("quantity", 0.0)
        unit_b = b_attr.get("unit", "")
        qty_str = f"_{int(qty_b)}{unit_b}" if qty_b > 0 else ""
        norm_key = normalize_title_for_matching(b["name"])
        family_key = (norm_key + canonical_pkg_b + qty_str) if norm_key else f"{b_attr['brand']}_{b_attr['product_type']}_{b_attr['variant']}{canonical_pkg_b}{qty_str}".strip("_")
        if not family_key:
            family_key = clean_canonical_key(b["name"]) + canonical_pkg_b + qty_str

        # Attach normalized attributes to b
        b_std = compute_standard_unit_price(b.get("price", 0.0), qty_b, unit_b)
        b["packaging"] = pkg_b
        b["quantity"] = qty_b
        b["unit"] = unit_b
        b["unit_price"] = b_attr.get("unit_price", 0.0)
        b["standard_unit_price"] = b_std["price_per_unit"]
        b["standard_unit"] = b_std["unit"]
        b["formatted_unit_price"] = b_std["formatted"]

        # Try to find an exact match in Zepto
        matched_z = None
        matched_z_idx = None
        for idx, (z, z_attr) in enumerate(z_attrs):
            if idx in matched_z_indices:
                continue

            # Task 3: Canonical Mapping Registry O(1) Lookup (bypasses fuzzy matching)
            z_id = str(z.get("id") or z.get("product_id") or "")
            b_id = str(b.get("id") or b.get("product_id") or "")
            if z_id and b_id and lookup_canonical_mapping(z_id, b_id):
                matched_z = z
                matched_z_idx = idx
                break

            if not check_packaging_guardrail(b["name"], z["name"], b.get("packsize", ""), z.get("packsize", "")):
                continue
            if not check_keyword_guardrails(b["name"], z["name"]):
                continue
            if are_products_exact_match(b_attr, z_attr):
                matched_z = z
                matched_z_idx = idx
                break

        z_std = None
        if matched_z is not None:
            z_matched_attr = z_attrs[matched_z_idx][1]
            matched_z["packaging"] = z_matched_attr.get("packaging", "")
            matched_z["quantity"] = z_matched_attr.get("quantity", 0.0)
            matched_z["unit"] = z_matched_attr.get("unit", "")
            matched_z["unit_price"] = z_matched_attr.get("unit_price", 0.0)
            z_std = compute_standard_unit_price(matched_z.get("price", 0.0), matched_z["quantity"], matched_z["unit"])
            matched_z["standard_unit_price"] = z_std["price_per_unit"]
            matched_z["standard_unit"] = z_std["unit"]
            matched_z["formatted_unit_price"] = z_std["formatted"]

        if family_key not in card_map:
            if matched_z is not None:
                matched_z_indices.add(matched_z_idx)

            cheapest = "Blinkit"
            savings = 0.0
            if matched_z:
                b_avail = b.get("is_available", True)
                z_avail = matched_z.get("is_available", True)
                if b_avail and z_avail:
                    bp = b["price"]
                    zp = matched_z["price"]
                    if zp < bp:
                        cheapest = "Zepto"
                        savings = round(bp - zp, 2)
                    elif bp < zp:
                        cheapest = "Blinkit"
                        savings = round(zp - bp, 2)
                    else:
                        cheapest = "Tie"
                        savings = 0.0
                elif b_avail:
                    cheapest = "Blinkit"
                    savings = 0.0
                elif z_avail:
                    cheapest = "Zepto"
                    savings = 0.0
                else:
                    cheapest = "None"
                    savings = 0.0

            std_unit = b_std["unit"] if b_std else (z_std["unit"] if z_std else "unit")
            card_map[family_key] = {
                "id": f"card_{len(card_map) + 1}",
                "canonical_name": b["name"],
                "brand": b_attr["brand"],
                "packaging": pkg_b,
                "quantity": qty_b,
                "unit": unit_b,
                "unit_price_comparison": {
                    "blinkit_unit_price": b.get("unit_price", 0.0),
                    "zepto_unit_price": matched_z.get("unit_price", 0.0) if matched_z else None,
                    "blinkit_standard_unit_price": b_std["price_per_unit"] if b_std else None,
                    "zepto_standard_unit_price": z_std["price_per_unit"] if z_std else None,
                    "standard_unit": std_unit,
                    "unit": unit_b or (matched_z.get("unit", "") if matched_z else ""),
                    "metric": f"₹/{std_unit}",
                    "formatted_blinkit": b_std["formatted"] if b_std else None,
                    "formatted_zepto": z_std["formatted"] if z_std else None,
                },
                "blinkit": b,
                "blinkit_variants": [],
                "zepto": matched_z,  # None if no exact match! (Out of Stock)
                "zepto_variants": [],
                "cheapest_store": cheapest if matched_z else ("Blinkit" if b.get("is_available", True) else "None"),
                "savings": savings
            }
        else:
            target_card = card_map[family_key]
            target_card["blinkit_variants"].append(b)
            if matched_z is not None:
                matched_z_indices.add(matched_z_idx)
                target_card["zepto_variants"].append(matched_z)

    # Process remaining Zepto items
    for idx, (z, z_attr) in enumerate(z_attrs):
        if idx in matched_z_indices:
            continue
        pkg_z = z_attr.get("packaging", "")
        canonical_pkg_z = "_tetra" if pkg_z == "tetra" else (f"_{pkg_z}" if pkg_z else "")
        qty_z = z_attr.get("quantity", 0.0)
        unit_z = z_attr.get("unit", "")
        qty_str = f"_{int(qty_z)}{unit_z}" if qty_z > 0 else ""
        norm_key = normalize_title_for_matching(z["name"])
        family_key = (norm_key + canonical_pkg_z + qty_str) if norm_key else f"{z_attr['brand']}_{z_attr['product_type']}_{z_attr['variant']}{canonical_pkg_z}{qty_str}".strip("_")
        if not family_key:
            family_key = clean_canonical_key(z["name"]) + canonical_pkg_z + qty_str

        z_std = compute_standard_unit_price(z.get("price", 0.0), qty_z, unit_z)
        z["packaging"] = pkg_z
        z["quantity"] = qty_z
        z["unit"] = unit_z
        z["unit_price"] = z_attr.get("unit_price", 0.0)
        z["standard_unit_price"] = z_std["price_per_unit"]
        z["standard_unit"] = z_std["unit"]
        z["formatted_unit_price"] = z_std["formatted"]

        if family_key in card_map:
            card_map[family_key]["zepto_variants"].append(z)
        else:
            card_map[family_key] = {
                "id": f"card_{len(card_map) + 1}",
                "canonical_name": z["name"],
                "brand": z_attr["brand"],
                "packaging": pkg_z,
                "quantity": qty_z,
                "unit": unit_z,
                "unit_price_comparison": {
                    "blinkit_unit_price": None,
                    "zepto_unit_price": z.get("unit_price", 0.0),
                    "blinkit_standard_unit_price": None,
                    "zepto_standard_unit_price": z_std["price_per_unit"],
                    "standard_unit": z_std["unit"],
                    "unit": unit_z,
                    "metric": f"₹/{z_std['unit']}",
                    "formatted_blinkit": None,
                    "formatted_zepto": z_std["formatted"],
                },
                "blinkit": None,
                "blinkit_variants": [],
                "zepto": z,
                "zepto_variants": [],
                "cheapest_store": "Zepto" if z.get("is_available", True) else "None",
                "savings": 0.0
            }

    # Second pass: for cards where primary zepto is None, check if any zepto_variant exactly matches the primary blinkit
    for card in card_map.values():
        b = card.get("blinkit")
        z = card.get("zepto")
        if b and not z and card["zepto_variants"]:
            b_attr = extract_product_attributes(b["name"], b.get("packsize", ""), b.get("price", 0.0))
            matched_v = None
            for zv in card["zepto_variants"]:
                zv_id = str(zv.get("id") or zv.get("product_id") or "")
                b_id = str(b.get("id") or b.get("product_id") or "")
                if zv_id and b_id and lookup_canonical_mapping(zv_id, b_id):
                    matched_v = zv
                    break
                if not check_packaging_guardrail(b["name"], zv["name"], b.get("packsize", ""), zv.get("packsize", "")):
                    continue
                if not check_keyword_guardrails(b["name"], zv["name"]):
                    continue
                zv_attr = extract_product_attributes(zv["name"], zv.get("packsize", ""), zv.get("price", 0.0))
                if are_products_exact_match(b_attr, zv_attr):
                    matched_v = zv
                    break
            if matched_v:
                card["zepto_variants"].remove(matched_v)
                card["zepto"] = matched_v
                v_std = compute_standard_unit_price(matched_v.get("price", 0.0), matched_v.get("quantity", 0.0), matched_v.get("unit", ""))
                card["unit_price_comparison"]["zepto_unit_price"] = matched_v.get("unit_price", 0.0)
                card["unit_price_comparison"]["zepto_standard_unit_price"] = v_std["price_per_unit"]
                card["unit_price_comparison"]["formatted_zepto"] = v_std["formatted"]
                if not card["unit_price_comparison"].get("standard_unit"):
                    card["unit_price_comparison"]["standard_unit"] = v_std["unit"]
                    card["unit_price_comparison"]["metric"] = f"₹/{v_std['unit']}"
                b_avail = b.get("is_available", True)
                zv_avail = matched_v.get("is_available", True)
                if b_avail and zv_avail:
                    bp = b["price"]
                    zp = matched_v["price"]
                    if zp < bp:
                        card["cheapest_store"] = "Zepto"
                        card["savings"] = round(bp - zp, 2)
                    elif bp < zp:
                        card["cheapest_store"] = "Blinkit"
                        card["savings"] = round(zp - bp, 2)
                    else:
                        card["cheapest_store"] = "Tie"
                        card["savings"] = 0.0
                elif b_avail:
                    card["cheapest_store"] = "Blinkit"
                    card["savings"] = 0.0
                elif zv_avail:
                    card["cheapest_store"] = "Zepto"
                    card["savings"] = 0.0
                else:
                    card["cheapest_store"] = "None"
                    card["savings"] = 0.0

    # Provider serviceability is a card-level state, not product stock.
    # This prevents a provider with zero products because it is unavailable
    # from being rendered as "Out of stock" on every comparison card.
    for card in card_map.values():
        card["blinkit_serviceability"] = blinkit_serviceability
        card["zepto_serviceability"] = zepto_serviceability

    return list(card_map.values())



def build_canonical_cards_with_instamart(
    blinkit_items: list,
    zepto_items: list,
    instamart_items: list,
    blinkit_serviceability: str = "serviceable",
    zepto_serviceability: str = "serviceable",
    instamart_serviceability: str = "serviceable",
) -> List[Dict[str, Any]]:
    """Build one canonical product card across Blinkit, Zepto and Instamart.

    ``build_canonical_cards`` remains untouched.  This three-provider layer is
    intentionally more tolerant than the legacy two-provider matcher because
    the three stores frequently format the same SKU differently.  It still
    hard-locks the things that must not be crossed: quantity, explicit
    packaging, brand (when both sides expose it), and incompatible categories.
    """

    def _clean_item(raw: Dict[str, Any], provider: str) -> Dict[str, Any]:
        item = dict(raw)
        name = str(item.get("name") or item.get("title") or item.get("raw_name") or "").strip()
        pack = str(item.get("packsize") or item.get("quantity_description") or item.get("quantity") or "").strip()
        try:
            raw_price = (
                item.get("price")
                or item.get("offerPrice")
                or item.get("selling_price")
                or item.get("discounted_price")
                or item.get("discountedSellingPrice")
                or item.get("sellingPrice")
                or item.get("finalPrice")
                or 0.0
            )
            if isinstance(raw_price, dict):
                raw_price = (
                    raw_price.get("value")
                    or raw_price.get("amount")
                    or raw_price.get("price")
                    or raw_price.get("finalPrice")
                    or 0.0
                )
            price = float(raw_price)
        except Exception:
            price = 0.0

        item["name"] = name
        item["raw_name"] = str(item.get("raw_name") or name)
        item["packsize"] = pack
        item["price"] = price
        item["store"] = provider.title() if provider != "instamart" else "Instamart"
        item["product_id"] = str(
            item.get("product_id") or item.get("id") or item.get("sku_id") or item.get("variant_id") or ""
        ).strip()
        item["id"] = str(item.get("id") or item.get("product_id") or "").strip()

        attr = extract_product_attributes(name, pack, price)
        explicit_brand = str(item.get("brand") or "").strip().lower()
        item["brand"] = explicit_brand or attr.get("brand", "")
        item["packaging"] = attr.get("packaging", "")
        item["quantity"] = float(attr.get("quantity") or 0.0)
        item["unit"] = str(attr.get("unit") or "")
        item["unit_price"] = float(attr.get("unit_price") or 0.0)

        std = compute_standard_unit_price(price, item["quantity"], item["unit"])
        item["standard_unit_price"] = float(std["price_per_unit"] or 0.0)
        item["standard_unit"] = str(std["unit"] or "")
        item["formatted_unit_price"] = str(item.get("formatted_unit_price") or std["formatted"] or "")

        if not item.get("is_available") and "is_available" not in item:
            item["is_available"] = not bool(item.get("out_of_stock", False))
        else:
            item["is_available"] = bool(item.get("is_available", not bool(item.get("out_of_stock", False))))

        # Keep a usable URL even when a provider omitted a detail URL.
        if not item.get("buy_url"):
            encoded = urllib.parse.quote(name)
            if provider == "blinkit":
                item["buy_url"] = f"https://blinkit.com/s/?q={encoded}"
            elif provider == "zepto":
                item["buy_url"] = f"https://www.zeptonow.com/search?query={encoded}"
            else:
                item["buy_url"] = f"https://www.swiggy.com/instamart/search?query={encoded}"

        return item

    def _norm_brand(item: Dict[str, Any]) -> str:
        brand = str(item.get("brand") or "").strip().lower()
        if not brand:
            attr = extract_product_attributes(item.get("name", ""), item.get("packsize", ""), item.get("price", 0.0))
            brand = str(attr.get("brand") or "").strip().lower()
        return re.sub(r"[^a-z0-9]+", " ", brand).strip()

    def _match_score(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[float]:
        """Return a confidence score for two provider SKUs.

        Matching is deliberately *variant aware*: brand + normalized quantity
        are hard locks, while harmless marketplace wording such as
        ``fresh``, ``pouch`` and ``pack`` is ignored.  The remaining product
        identity words are compared with several signals so titles like
        ``Amul Taaza Toned Milk`` and ``Amul Taaza Toned Fresh Milk | Pouch``
        match, while ``Amul Taaza 500 ml`` and ``Amul Gold 500 ml`` do not.
        """
        name_a = str(a.get("name") or "").strip()
        name_b = str(b.get("name") or "").strip()
        pack_a = str(a.get("packsize") or "").strip()
        pack_b = str(b.get("packsize") or "").strip()
        if not name_a or not name_b:
            return None

        # --------------------------- hard locks ---------------------------
        qty_a, unit_a = extract_normalized_quantity(name_a, pack_a)
        qty_b, unit_b = extract_normalized_quantity(name_b, pack_b)

        if qty_a > 0 and qty_b > 0:
            if unit_a != unit_b:
                return None
            rel = abs(qty_a - qty_b) / max(qty_a, qty_b)
            if rel > 0.01:
                return None
        elif qty_a != qty_b:
            # If only one side has a measurable quantity, do not invent a
            # match. Unknown quantity is allowed only when both are unknown.
            return None

        brand_a = _norm_brand(a)
        brand_b = _norm_brand(b)
        if brand_a and brand_b:
            if brand_a != brand_b and brand_a not in brand_b and brand_b not in brand_a:
                return None
        elif brand_a != brand_b:
            return None

        # Explicit incompatible categories / packaging remain hard rejects.
        if not check_keyword_guardrails(name_a, name_b):
            return None
        if not check_packaging_guardrail(name_a, name_b, pack_a, pack_b):
            return None

        attr_a = extract_product_attributes(name_a, pack_a, float(a.get("price") or 0.0))
        attr_b = extract_product_attributes(name_b, pack_b, float(b.get("price") or 0.0))

        type_a = str(attr_a.get("product_type") or "").strip().lower()
        type_b = str(attr_b.get("product_type") or "").strip().lower()
        if type_a and type_b and type_a != "general" and type_b != "general":
            if type_a != type_b and type_a not in type_b and type_b not in type_a:
                return None

        # ---------------------- identity normalization -------------------
        # normalize_title_for_matching() is intentionally the primary
        # normalization because it already removes marketplace filler while
        # preserving meaningful variant words such as gold/toned/lactose.
        na = normalize_title_for_matching(name_a)
        nb = normalize_title_for_matching(name_b)
        if not na or not nb:
            return None

        # Build a second residual representation that strips only known
        # merchandising/filler tokens.  This catches cases where one provider
        # adds words such as "fresh", "pouch", "pack", "full cream", etc.
        def residual_tokens(name: str, pack: str, brand: str) -> set[str]:
            text = strip_accents(f"{name} {pack}").lower()
            if brand:
                text = re.sub(r"\b" + re.escape(brand.lower()) + r"\b", " ", text)
            text = re.sub(
                r"\b\d+(?:\.\d+)?\s*(?:ml|milli|mls|millilitre|millilitres|milliliter|"
                r"l|ltr|litre|litres|liter|liters|kg|kgs|kilo|kilogram|g|gm|gms|grams?|"
                r"pc|pcs|piece|pieces|pack|packs|unit|units)\b",
                " ", text,
            )
            text = re.sub(r"\b\d+\s*[xX*]\s*\d+(?:\.\d+)?\s*\w+\b", " ", text)
            text = re.sub(
                r"\b(?:fresh|pure|original|natural|best|premium|new|daily|everyday|"
                r"pouch|pack|packet|pcs?|pieces?|tetra|tetrapack|tetra\s*pack|"
                r"bottle|pet|carton|box|tin|can|jar|brik|brick|pasteurised|homogenised)\b",
                " ", text,
            )
            text = re.sub(r"[^a-z0-9]+", " ", text)
            return {t for t in text.split() if len(t) >= 2}

        ta = residual_tokens(name_a, pack_a, brand_a)
        tb = residual_tokens(name_b, pack_b, brand_b)

        # Variant-conflict guard: if both titles expose meaningful variant
        # descriptors, require the descriptors to agree. This prevents same-
        # brand flavour/recipe SKUs from collapsing merely because brand,
        # category, and quantity are equal.
        def variant_residual(tokens: set[str]) -> set[str]:
            generic = {
                "product", "products", "food", "snack", "snacks", "chips", "chip", "potato", "crisps", "crisp",
                "namkeen", "puffs", "puff", "balls", "ball", "rings", "ring", "sticks", "stick", "mix", "mixture",
                "milk", "water", "juice", "drink", "beverage", "biscuit", "biscuits", "cookie", "cookies",
                "chocolate", "bar", "bars", "candy", "treat", "classic", "original", "natural", "premium",
                "fresh", "pure", "healthy", "tasty", "crunchy", "spicy", "flavour", "flavor", "flavoured",
                "style", "made", "from", "with", "and", "the", "no", "less", "fat", "fats", "palm", "oil",
                "fried", "baked", "roasted", "multigrain", "protein", "contains", "antioxidants", "daily",
                "everyday", "pack", "pouch", "packet", "bottle", "box", "carton", "tetra", "unit",
            }
            return {t for t in tokens if t not in generic and len(t) >= 3}

        variant_a = variant_residual(ta)
        variant_b = variant_residual(tb)
        if variant_a and variant_b:
            # One title may contain extra merchandising descriptors, but the
            # actual variant descriptors must agree. A subset relationship
            # allows "sour cream onion" vs "cream onion" while rejecting
            # "west indies chilli" vs "sizzling hot" and "banana" vs
            # "masala magic".
            if not (variant_a.issubset(variant_b) or variant_b.issubset(variant_a)):
                return None
        if not ta or not tb:
            return None

        shared = ta & tb
        union = ta | tb
        overlap = len(shared) / max(1, min(len(ta), len(tb)))
        coverage = len(shared) / max(1, len(union))

        token_set = float(rapidfuzz.fuzz.token_set_ratio(na, nb))
        token_sort = float(rapidfuzz.fuzz.token_sort_ratio(na, nb))
        ratio = float(rapidfuzz.fuzz.ratio(na, nb))

        # Exact normalized identity after filler removal.
        if na == nb or ta == tb:
            return 100.0

        # Strong containment is common when Zepto adds merchandising words.
        if shared and (ta.issubset(tb) or tb.issubset(ta)):
            if overlap >= 1.0:
                return 97.0
            if overlap >= 0.75:
                return 94.0

        # High-confidence fuzzy match.  Token-set is preferred because word
        # order and extra provider descriptors should not matter.
        if token_set >= 88.0:
            return round(token_set, 2)

        # For short product names, require actual shared identity tokens rather
        # than trusting a high fuzzy score caused by a single common word.
        if len(shared) >= 2 and overlap >= 0.67 and coverage >= 0.50:
            score = max(token_set, token_sort, ratio, 72.0)
            return round(score, 2)

        # Brand + product type + one substantial variant token can still be a
        # valid cross-provider match when the remaining title differs heavily.
        # Do not use this fallback for generic one-word products.
        if brand_a and brand_b and brand_a == brand_b:
            meaningful_shared = {t for t in shared if t not in {type_a, type_b, "milk", "water", "juice"}}
            if type_a == type_b and meaningful_shared:
                return round(max(72.0, token_set), 2)

        return None

    def _decorate_card(card: Dict[str, Any]) -> None:
        offers = []
        for provider in ("blinkit", "zepto", "instamart"):
            item = card.get(provider)
            if isinstance(item, dict) and item.get("is_available", True) and float(item.get("price") or 0.0) > 0:
                offers.append((provider.title() if provider != "instamart" else "Instamart", float(item["price"])))

        if offers:
            min_price = min(p for _, p in offers)
            max_price = max(p for _, p in offers)
            winners = [store for store, price in offers if abs(price - min_price) < 0.005]
            card["cheapest_store"] = winners[0] if len(winners) == 1 else "Tie"
            card["savings"] = round(max_price - min_price, 2) if len(offers) >= 2 else 0.0
        else:
            card["cheapest_store"] = "None"
            card["savings"] = 0.0

        up = card.setdefault("unit_price_comparison", {})
        for provider in ("blinkit", "zepto", "instamart"):
            item = card.get(provider)
            key = provider
            up.setdefault(f"{key}_unit_price", float(item.get("unit_price") or 0.0) if isinstance(item, dict) else None)
            up.setdefault(f"{key}_standard_unit_price", float(item.get("standard_unit_price") or 0.0) if isinstance(item, dict) else None)
            up.setdefault(f"formatted_{key}", item.get("formatted_unit_price") if isinstance(item, dict) else None)

        if not up.get("standard_unit"):
            for provider in ("blinkit", "zepto", "instamart"):
                item = card.get(provider)
                if isinstance(item, dict) and item.get("standard_unit"):
                    up["standard_unit"] = item["standard_unit"]
                    up["metric"] = f"₹/{item['standard_unit']}"
                    break

    def _new_card(item: Dict[str, Any], provider: str) -> Dict[str, Any]:
        return {
            "id": f"card_{uuid.uuid4().hex[:12]}",
            "canonical_name": item.get("name", ""),
            "brand": item.get("brand", ""),
            "packaging": item.get("packaging", ""),
            "quantity": item.get("quantity", 0.0),
            "unit": item.get("unit", ""),
            "unit_price_comparison": {},
            "blinkit": None,
            "blinkit_variants": [],
            "zepto": None,
            "zepto_variants": [],
            "instamart": None,
            "instamart_variants": [],
            "blinkit_serviceability": blinkit_serviceability,
            "zepto_serviceability": zepto_serviceability,
            "instamart_serviceability": instamart_serviceability,
        }

    b_items = [_clean_item(x, "blinkit") for x in (blinkit_items or []) if isinstance(x, dict)]
    z_items = [_clean_item(x, "zepto") for x in (zepto_items or []) if isinstance(x, dict)]
    i_items = [_clean_item(x, "instamart") for x in (instamart_items or []) if isinstance(x, dict)]

    cards: List[Dict[str, Any]] = []

    def _attach(card: Dict[str, Any], item: Dict[str, Any], provider: str) -> None:
        if card.get(provider) is None:
            card[provider] = item
        else:
            variants = card.setdefault(f"{provider}_variants", [])
            item_id = str(item.get("product_id") or item.get("id") or "")
            existing_ids = {
                str(x.get("product_id") or x.get("id") or "")
                for x in variants if isinstance(x, dict)
            }
            primary_id = str(card[provider].get("product_id") or card[provider].get("id") or "")
            if item_id and item_id != primary_id and item_id not in existing_ids:
                variants.append(item)
        _decorate_card(card)

    # Preserve provider arrival order at the card level: Blinkit products seed
    # the queue, then Zepto/Instamart can enrich or append to it.
    for item in b_items:
        card = _new_card(item, "blinkit")
        _attach(card, item, "blinkit")
        cards.append(card)

    def _match_into_existing(items: List[Dict[str, Any]], provider: str) -> None:
        used_ids: set[str] = set()
        for item in items:
            item_id = str(item.get("product_id") or item.get("id") or "")
            best_card = None
            best_score = -1.0
            for card in cards:
                # A provider SKU already attached to a card cannot be reused.
                attached_ids = set()
                primary = card.get(provider)
                if isinstance(primary, dict):
                    attached_ids.add(str(primary.get("product_id") or primary.get("id") or ""))
                for variant in card.get(f"{provider}_variants") or []:
                    if isinstance(variant, dict):
                        attached_ids.add(str(variant.get("product_id") or variant.get("id") or ""))
                if item_id and item_id in attached_ids:
                    continue

                for base_provider in ("blinkit", "zepto", "instamart"):
                    if base_provider == provider:
                        continue
                    base_items = []
                    primary = card.get(base_provider)
                    if isinstance(primary, dict):
                        base_items.append(primary)
                    base_items.extend(x for x in card.get(f"{base_provider}_variants") or [] if isinstance(x, dict))
                    for base in base_items:
                        score = _match_score(item, base)
                        if score is not None and score > best_score:
                            best_score = score
                            best_card = card

            if best_card is not None and best_score >= 72.0:
                _attach(best_card, item, provider)
                if item_id:
                    used_ids.add(item_id)
            else:
                card = _new_card(item, provider)
                _attach(card, item, provider)
                cards.append(card)

    _match_into_existing(z_items, "zepto")
    _match_into_existing(i_items, "instamart")

    for card in cards:
        card["blinkit_serviceability"] = blinkit_serviceability
        card["zepto_serviceability"] = zepto_serviceability
        card["instamart_serviceability"] = instamart_serviceability
        _decorate_card(card)

    return cards


async def _do_compare_prices(
    cleaned_q: str,
    resolved_lat: str,
    resolved_lon: str,
    x_zepto_cookie: Optional[str] = None,
    x_app_session_id: Optional[str] = None,
    client_zepto_products: Optional[List[Any]] = None
) -> Dict[str, Any]:
    f_lat = float(resolved_lat)
    f_lon = float(resolved_lon)

    # Fetch all live providers concurrently for the legacy /api/compare path too.
    blinkit_task = asyncio.create_task(fetch_blinkit_products(cleaned_q, f_lat, f_lon))
    instamart_task = asyncio.create_task(fetch_instamart_products(cleaned_q, f_lat, f_lon))

    
    zepto_error = None

    # Zepto:
    # - If the Flutter client already supplied real Zepto products, use them.
    # - Otherwise fetch Zepto server-side using the user's location and the
    #   dynamically resolved local store.
    if client_zepto_products is not None and (
        bool(client_zepto_products)
        if not hasattr(client_zepto_products, "__len__")
        else len(client_zepto_products) > 0
    ):
        logger.info("Using client-side offloaded Zepto payload")

        from zepto_service import extract_products

        try:
            if isinstance(client_zepto_products, str):
                parsed_zepto = json.loads(client_zepto_products)
            else:
                parsed_zepto = client_zepto_products

            if (
                isinstance(parsed_zepto, list)
                and len(parsed_zepto) > 0
                and isinstance(parsed_zepto[0], dict)
                and (
                    "price" in parsed_zepto[0]
                    or "discounted_price" in parsed_zepto[0]
                )
            ):
                zepto_items = []

                for p in parsed_zepto:
                    p_clean = dict(p)

                    pr = float(
                        p_clean.get("price")
                        or p_clean.get("discounted_price")
                        or 0.0
                    )

                    p_clean["price"] = pr
                    p_clean["discounted_price"] = pr
                    p_clean["mrp"] = float(
                        p_clean.get("mrp") or pr
                    )
                    p_clean["name"] = str(
                        p_clean.get("name") or ""
                    )
                    p_clean["packsize"] = str(
                        p_clean.get("packsize") or "1 unit"
                    )
                    p_clean["image_url"] = str(
                        p_clean.get("image_url") or ""
                    )
                    p_clean["is_available"] = bool(
                        p_clean.get("is_available", True)
                    )
                    p_clean["store"] = "Zepto"
                    p_clean["id"] = str(
                        p_clean.get("id")
                        or p_clean.get("product_id")
                        or ""
                    )
                    p_clean["product_id"] = p_clean["id"]
                    p_clean["buy_url"] = str(
                        p_clean.get("buy_url") or ""
                    )

                    zepto_items.append(p_clean)
            else:
                zepto_items = extract_products(parsed_zepto)

            logger.info(
                "Extracted %d products from client-side Zepto payload",
                len(zepto_items),
            )

            if not zepto_items:
                zepto_error = "No Zepto products returned"

        except Exception as e:
            logger.error(
                "Error parsing client-side Zepto payload: %s",
                type(e).__name__,
            )
            zepto_items = []
            zepto_error = "Payload parsing error"

        blinkit_res = await asyncio.gather(
            blinkit_task,
            instamart_task,
            return_exceptions=True,
        )

        blinkit_items = blinkit_res[0] if isinstance(blinkit_res[0], list) else []
        instamart_items = blinkit_res[1] if isinstance(blinkit_res[1], list) else []

        if isinstance(blinkit_res[0], Exception):
            logger.error("Blinkit async fetcher error: %s", type(blinkit_res[0]).__name__)
        if isinstance(blinkit_res[1], Exception):
            logger.error("Instamart async fetcher error: %s", type(blinkit_res[1]).__name__)

    else:
        logger.info(
            "No client-side Zepto payload; fetching Zepto "
            "server-side using dynamic location/store resolution."
        )

        zepto_task = asyncio.create_task(
            fetch_zepto_products(
                cleaned_q,
                f_lat,
                f_lon,
            )
        )

        results = await asyncio.gather(
            blinkit_task,
            zepto_task,
            instamart_task,
            return_exceptions=True,
        )

        blinkit_items = results[0] if isinstance(results[0], list) else []
        zepto_items = results[1] if isinstance(results[1], list) else []
        instamart_items = results[2] if isinstance(results[2], list) else []

        if isinstance(results[0], Exception):
            logger.error(
                "Blinkit async fetcher error: %s",
                type(results[0]).__name__,
            )

        if isinstance(results[1], Exception):
            logger.error(
                "Zepto async fetcher error: %s",
                type(results[1]).__name__,
            )
            zepto_items = []
        if isinstance(results[2], Exception):
            logger.error(
                "Instamart async fetcher error: %s",
                type(results[2]).__name__,
            )
            instamart_items = []

        if not zepto_items:
            zepto_error = "No Zepto products returned"

    def _fmt_b(b):
        p = b.get("packsize") or b.get("unit", "")
        c = calculate_unit_price(b["name"], p, b["price"])
        return {
            "name": b["name"],
            "packsize": p,
            "price": b["price"],
            "normalized_price": c["normalized_price"],
            "unit": c["unit"],
            "formatted_unit_price": c["formatted"],
            "image_url": b.get("image_url", ""),
            "buy_url": b.get("buy_url") or f"https://blinkit.com/s/?q={urllib.parse.quote(b['name'])}",
            "is_available": b.get("is_available", True)
        }

    def _fmt_z(z):
        p = z.get("packsize") or z.get("unit", "")
        c = calculate_unit_price(z["name"], p, z["price"])
        return {
            "name": z["name"],
            "packsize": p,
            "price": z["price"],
            "normalized_price": z.get("normalized_price") or c["normalized_price"],
            "unit": z.get("unit") or c["unit"],
            "formatted_unit_price": z.get("formatted_unit_price") or c["formatted"],
            "image_url": z.get("image_url", ""),
            "buy_url": z.get("buy_url") or f"https://www.zeptonow.com/search?query={urllib.parse.quote(z['name'])}",
            "is_available": z.get("is_available", True)
        }

    def _fmt_i(i):
        p = i.get("packsize") or i.get("unit", "")
        c = calculate_unit_price(i["name"], p, i["price"])
        return {
            "name": i["name"],
            "packsize": p,
            "price": i["price"],
            "normalized_price": c["normalized_price"],
            "unit": c["unit"],
            "formatted_unit_price": i.get("formatted_unit_price") or c["formatted"],
            "image_url": i.get("image_url", ""),
            "buy_url": i.get("buy_url") or f"https://www.swiggy.com/instamart/search?query={urllib.parse.quote(i['name'])}",
            "is_available": i.get("is_available", True)
        }

    def _pick_cheapest(bo, zo, io):
        stores = []
        if bo and bo.get("price", 0) >= 10:
            stores.append(("Blinkit", bo.get("normalized_price", bo["price"])))
        if zo and zo.get("price", 0) >= 10:
            stores.append(("Zepto", zo.get("normalized_price", zo["price"])))
        if io and io.get("price", 0) >= 10:
            stores.append(("Instamart", io.get("normalized_price", io["price"])))
        if not stores:
            return "None"
        stores.sort(key=lambda x: x[1])
        if len(stores) >= 2 and abs(stores[0][1] - stores[1][1]) < 0.01:
            return "Equal"
        return stores[0][0]

    paired_results = []
    used_zepto = set()

    # Pre-extract 4 key attributes for strict constraint enforcement
    b_attrs = [extract_product_attributes(b["name"], b.get("packsize", "")) for b in blinkit_items]
    z_attrs = [extract_product_attributes(z["name"], z.get("packsize", "")) for z in zepto_items]

    # 1. Iterate Blinkit items: find canonical SKU mapping first, then 3-stage entity match
    for b_idx, b_item in enumerate(blinkit_items):
        b_off = _fmt_b(b_item)
        b_attr = b_attrs[b_idx]

        matched_z_idx = None
        matched_canonical_sku = None
        b_id = str(b_item.get("id") or b_item.get("product_id") or b_item.get("item_id") or "").strip()

        # Step 1: Check canonical_mappings registry first (bypasses fuzzy matching)
        if b_id:
            for z_idx, z_item in enumerate(zepto_items):
                if z_idx in used_zepto:
                    continue
                z_id = str(z_item.get("id") or z_item.get("product_id") or z_item.get("item_id") or "").strip()
                if z_id:
                    c_sku = lookup_canonical_mapping(z_id, b_id)
                    if c_sku:
                        matched_z_idx = z_idx
                        matched_canonical_sku = c_sku
                        logger.info(f"Canonical mapping hit: ({z_id}, {b_id}) -> {c_sku}")
                        break

        # Step 2: Fallback to 3-stage entity matcher / 4-attribute guardrail
        if matched_z_idx is None:
            for z_idx, z_item in enumerate(zepto_items):
                if z_idx in used_zepto:
                    continue
                if match_products(b_item, z_item) is not None or are_products_exact_match(b_attr, z_attrs[z_idx]):
                    matched_z_idx = z_idx
                    break

        z_off = None
        if matched_z_idx is not None:
            used_zepto.add(matched_z_idx)
            z_off = _fmt_z(zepto_items[matched_z_idx])

        cheaper = _pick_cheapest(b_off, z_off, None)
        paired_results.append({
            "name": b_off["name"],
            "packsize": b_off["packsize"] or (z_off["packsize"] if z_off else ""),
            "unit": b_off["unit"],
            "match_score": 100 if z_off else 0,
            "canonical_sku_id": matched_canonical_sku,
            "cheaper_store": cheaper,
            "blinkit": b_off,
            "zepto": z_off,
            "instamart": None
        })

    # 2. Append remaining un-matched Zepto items
    for z_idx, z_item in enumerate(zepto_items):
        if z_idx in used_zepto:
            continue
        used_zepto.add(z_idx)
        z_off = _fmt_z(z_item)
        cheaper = _pick_cheapest(None, z_off, None)
        paired_results.append({
            "name": z_off["name"],
            "packsize": z_off["packsize"],
            "unit": z_off["unit"],
            "match_score": 0,
            "cheaper_store": cheaper,
            "blinkit": None,
            "zepto": z_off,
            "instamart": None
        })

    canonical_cards = build_canonical_cards_with_instamart(
        blinkit_items,
        zepto_items,
        instamart_items,
    )

    response_payload = {
        "query": cleaned_q,
        "results": paired_results,
        "canonical_cards": canonical_cards,
        "raw_blinkit_count": len(blinkit_items),
        "raw_zepto_count": len(zepto_items),
        "raw_instamart_count": len(instamart_items),
        "zepto_items": zepto_items,
        "instamart_items": instamart_items,
        "client_zepto_processed": len(zepto_items) > 0,
        "server_received_zepto_products": len(zepto_items) > 0,
        "zepto_blocked": len(zepto_items) == 0,
        "zepto_error": zepto_error if len(zepto_items) == 0 else None,
        "has_zepto_cookie": bool(x_zepto_cookie),
    }

    return response_payload


@app.get("/api/compare")
async def compare_prices(
    q: Optional[str] = Query(None, description="Search query"),
    query: Optional[str] = Query(None, description="Search query alias"),
    lat: Optional[str] = Query(None, description="User latitude"),
    lon: Optional[str] = Query(None, description="User longitude"),
    lng: Optional[str] = Query(None, description="User longitude alias"),
    latitude: Optional[str] = Query(None, description="User latitude alias"),
    longitude: Optional[str] = Query(None, description="User longitude alias"),
    x_location_lat: Optional[str] = Header(None, alias="X-Location-Lat"),
    x_location_lng: Optional[str] = Header(None, alias="X-Location-Lng"),
    h_lat: Optional[str] = Header(None, alias="latitude"),
    h_lon: Optional[str] = Header(None, alias="longitude"),
    h_lat_short: Optional[str] = Header(None, alias="lat"),
    h_lng_short: Optional[str] = Header(None, alias="lng"),
    x_zepto_cookie: Optional[str] = Header(None, alias="X-Zepto-Cookie"),
    x_app_session_id: Optional[str] = Header(None, alias="X-App-Session-Id") # Track active auth sessions
):
    """
    GET /api/compare:
    Fires both live scrapers concurrently via asyncio.gather.
    Fuzzy-matches items using thefuzz.token_sort_ratio with threshold >= 60.
    Compares normalized unit prices.
    Accepts X-Zepto-Cookie from client WebView to bypass AWS WAF.
    """
    cleaned_q = (q or query or "").strip()
    if not cleaned_q:
        return {"query": "", "results": [], "canonical_cards": [], "raw_blinkit_count": 0, "raw_zepto_count": 0, "zepto_blocked": False}

    resolved_lat = _clean_str(lat) or _clean_str(latitude) or _clean_str(x_location_lat) or _clean_str(h_lat) or _clean_str(h_lat_short)
    resolved_lon = _clean_str(lon) or _clean_str(lng) or _clean_str(longitude) or _clean_str(x_location_lng) or _clean_str(h_lon) or _clean_str(h_lng_short)
    if not resolved_lat or not resolved_lon:
        return {"query": cleaned_q, "results": [], "raw_blinkit_count": 0, "raw_zepto_count": 0, "raw_instamart_count": 0, "error": "Location coordinates required"}

    return await _do_compare_prices(cleaned_q, resolved_lat, resolved_lon, x_zepto_cookie, x_app_session_id)


class CompareRequest(BaseModel):
    query: str
    latitude: float
    longitude: float
    zepto_products: Optional[Any] = []
    zepto_cookies: Optional[str] = None


ComparePostRequest = CompareRequest


@app.post("/api/compare")
async def compare_prices_post(
    payload: CompareRequest,
    x_location_lat: Optional[str] = Header(None, alias="X-Location-Lat"),
    x_location_lng: Optional[str] = Header(None, alias="X-Location-Lng"),
    x_zepto_cookie: Optional[str] = Header(None, alias="X-Zepto-Cookie"),
    x_app_session_id: Optional[str] = Header(None, alias="X-App-Session-Id")
):
    """
    POST /api/compare:
    Accepts search query, user coordinates, and optional client-side offloaded zepto_products or zepto_cookies.
    Fetches Blinkit server-side, performs fuzzy matching & canonical SKU pairing, and returns combined price comparison.
    """
    cleaned_q = payload.query.strip()
    if not cleaned_q:
        return {"query": "", "results": [], "canonical_cards": [], "raw_blinkit_count": 0, "raw_zepto_count": 0, "zepto_blocked": False}

    resolved_lat = _clean_str(payload.latitude)
    resolved_lon = _clean_str(payload.longitude)
    if not resolved_lat or not resolved_lon:
        return {"query": cleaned_q, "results": [], "raw_blinkit_count": 0, "raw_zepto_count": 0, "raw_instamart_count": 0, "error": "Location coordinates required"}

    cookie_to_use = payload.zepto_cookies or x_zepto_cookie
    return await _do_compare_prices(
        cleaned_q,
        resolved_lat,
        resolved_lon,
        cookie_to_use,
        x_app_session_id,
        client_zepto_products=payload.zepto_products
    )


@app.get("/api/canonical")
async def get_canonical_products(
    q: str = Query(..., description="Query"),
    lat: Optional[str] = Query(None),
    lon: Optional[str] = Query(None),
    latitude: Optional[str] = Query(None),
    longitude: Optional[str] = Query(None),
    x_location_lat: Optional[str] = Header(None, alias="X-Location-Lat"),
    x_location_lng: Optional[str] = Header(None, alias="X-Location-Lng"),
    h_lat: Optional[str] = Header(None, alias="latitude"),
    h_lon: Optional[str] = Header(None, alias="longitude"),
    h_lat_short: Optional[str] = Header(None, alias="lat"),
    h_lng_short: Optional[str] = Header(None, alias="lng")
):
    cleaned_q = q.strip()
    if not cleaned_q:
        return {"query": q, "cards": [], "count": 0}
    resolved_lat = _clean_str(lat) or _clean_str(latitude) or _clean_str(x_location_lat) or _clean_str(h_lat) or _clean_str(h_lat_short)
    resolved_lon = _clean_str(lon) or _clean_str(longitude) or _clean_str(x_location_lng) or _clean_str(h_lon) or _clean_str(h_lng_short)
    if not resolved_lat or not resolved_lon:
        return {"query": q, "cards": [], "count": 0, "error": "Location coordinates required"}

    f_lat = float(resolved_lat)
    f_lon = float(resolved_lon)
    try:
        store_id = await resolve_zepto_store_id(f_lat, f_lon)
    except Exception:
        store_id = await resolve_dark_store_id(f_lat, f_lon)

    # Short-Lived Store Caching (key format: cache:<store_id>:<query_hash>:canonical, TTL 300s)
    query_hash = hashlib.sha256(cleaned_q.lower().strip().encode("utf-8")).hexdigest()[:16]
    canon_cache_key = f"cache:{store_id}:{query_hash}:canonical"

    if canon_cache_key in RAM_STORE_QUERY_CACHE:
        logger.info(f"Canonical store cache hit (RAM) for {canon_cache_key}")
        hit = dict(RAM_STORE_QUERY_CACHE[canon_cache_key])
        hit["cached"] = True
        return hit

    if async_redis_client is not None:
        try:
            cached_val = await asyncio.wait_for(async_redis_client.get(canon_cache_key), timeout=0.3)
            if cached_val:
                logger.info(f"Canonical store cache hit (Redis) for {canon_cache_key}")
                parsed = json.loads(cached_val)
                parsed["cached"] = True
                RAM_STORE_QUERY_CACHE[canon_cache_key] = parsed
                return parsed
        except Exception as e:
            logger.debug(f"Redis canonical cache lookup error: {e}")

    blinkit_task = asyncio.create_task(fetch_blinkit_products(cleaned_q, f_lat, f_lon, store_id))
    zepto_task = asyncio.create_task(fetch_zepto_products(cleaned_q, f_lat, f_lon, store_id))
    instamart_task = asyncio.create_task(fetch_instamart_products(cleaned_q, f_lat, f_lon))
    results = await asyncio.gather(blinkit_task, zepto_task, instamart_task, return_exceptions=True)
    blinkit_items = results[0] if isinstance(results[0], list) else []
    zepto_items = results[1] if isinstance(results[1], list) else []
    instamart_items = results[2] if isinstance(results[2], list) else []
    if isinstance(results[0], Exception):
        logger.error(f"Blinkit async fetcher error: {results[0]}")
    if isinstance(results[1], Exception):
        logger.error(f"Zepto async fetcher error: {results[1]}")
    cards = build_canonical_cards_with_instamart(blinkit_items, zepto_items, instamart_items)
    canon_response = {
        "query": cleaned_q,
        "cards": cards,
        "count": len(cards),
        "raw_blinkit_count": len(blinkit_items),
        "raw_zepto_count": len(zepto_items),
        "raw_instamart_count": len(instamart_items),
        "store_id": str(store_id),
        "cache_key": canon_cache_key
    }

    RAM_STORE_QUERY_CACHE[canon_cache_key] = canon_response
    if async_redis_client is not None:
        try:
            await asyncio.wait_for(
                async_redis_client.setex(canon_cache_key, STORE_CACHE_TTL, json.dumps(canon_response)),
                timeout=0.4
            )
        except Exception as e:
            logger.debug(f"Redis store cache write error: {e}")

    return canon_response


class CanonicalMappingRequest(BaseModel):
    zepto_item_id: str
    blinkit_item_id: str
    canonical_sku_id: str


@app.post("/api/canonical-mappings")
async def add_canonical_mapping(req: CanonicalMappingRequest):
    """Register verified canonical SKU mapping stored in-memory and SQLite."""
    register_canonical_mapping(req.zepto_item_id, req.blinkit_item_id, req.canonical_sku_id)
    return {
        "status": "success",
        "zepto_item_id": req.zepto_item_id,
        "blinkit_item_id": req.blinkit_item_id,
        "canonical_sku_id": req.canonical_sku_id
    }


@app.get("/api/canonical-mappings")
async def get_canonical_mapping(zepto_item_id: str = Query(...), blinkit_item_id: str = Query(...)):
    """O(1) lookup for canonical SKU mapping."""
    res = lookup_canonical_mapping(zepto_item_id, blinkit_item_id)
    return {
        "zepto_item_id": zepto_item_id,
        "blinkit_item_id": blinkit_item_id,
        "canonical_sku_id": res,
        "found": res is not None
    }


@app.get("/api/zepto")
async def get_zepto_products(
    q: str = Query(..., description="Query"),
    lat: Optional[str] = Query(None),
    lon: Optional[str] = Query(None),
    latitude: Optional[str] = Query(None),
    longitude: Optional[str] = Query(None),
    x_location_lat: Optional[str] = Header(None, alias="X-Location-Lat"),
    x_location_lng: Optional[str] = Header(None, alias="X-Location-Lng"),
    h_lat: Optional[str] = Header(None, alias="latitude"),
    h_lon: Optional[str] = Header(None, alias="longitude"),
    h_lat_short: Optional[str] = Header(None, alias="lat"),
    h_lng_short: Optional[str] = Header(None, alias="lng"),
    x_zepto_cookie: Optional[str] = Header(None, alias="X-Zepto-Cookie")
):
    cleaned_q = q.strip()
    if not cleaned_q:
        return {"items": [], "count": 0}
    raw_lat = _clean_str(lat) or _clean_str(latitude) or _clean_str(x_location_lat) or _clean_str(h_lat) or _clean_str(h_lat_short)
    raw_lon = _clean_str(lon) or _clean_str(longitude) or _clean_str(x_location_lng) or _clean_str(h_lon) or _clean_str(h_lng_short)
    if not raw_lat or not raw_lon:
        return {"items": [], "count": 0, "error": "Location coordinates required"}
    try:
        resolved_lat = float(raw_lat)
        resolved_lon = float(raw_lon)
    except ValueError:
        return {"items": [], "count": 0, "error": "Invalid location coordinates"}
    try:
        store_id = await resolve_zepto_store_id(resolved_lat, resolved_lon)
    except Exception:
        store_id = await resolve_dark_store_id(resolved_lat, resolved_lon)
    items = await fetch_zepto_products(cleaned_q, resolved_lat, resolved_lon, store_id=store_id, client_cookie=x_zepto_cookie)
    return {"items": items, "count": len(items)}


@app.post("/api/get-prices")
async def get_prices(payload: PriceRequest, x_zepto_cookie: Optional[str] = Header(None, alias="X-Zepto-Cookie")):
    """
    Accepts dynamic GPS coordinates (latitude, longitude) and search query via JSON.
    Directly extracts live product payloads from Zepto asynchronously.
    """
    q = payload.query.strip()
    if not q:
        return {
            "query": q,
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "count": 0,
            "products": []
        }

    try:
        store_id = await resolve_zepto_store_id(float(payload.latitude), float(payload.longitude))
    except Exception:
        store_id = await resolve_dark_store_id(float(payload.latitude), float(payload.longitude))
    products = await fetch_zepto_products(q, float(payload.latitude), float(payload.longitude), store_id=store_id, client_cookie=x_zepto_cookie)
    return {
        "query": q,
        "latitude": float(payload.latitude),
        "longitude": float(payload.longitude),
        "count": len(products),
        "products": products
    }


@app.get("/api/zepto-status")
def zepto_status(x_zepto_cookie: Optional[str] = Header(None, alias="X-Zepto-Cookie")):
    cookie_str = x_zepto_cookie if isinstance(x_zepto_cookie, str) else None
    return {
        "status": "ready",
        "engine": "SeleniumBase UC Mode",
        "mobile_emulation": True,
        "has_client_cookie": bool(cookie_str),
        "cookie_length": len(cookie_str) if cookie_str else 0
    }


@app.post("/api/debug_zepto")
async def debug_zepto(payload: Dict[str, Any]):
    query = payload.get("query", "")
    count = payload.get("count", 0)
    items = payload.get("items", [])
    logger.info(f"[DEBUG_ZEPTO] Received {count} items for query '{query}':")
    for idx, item in enumerate(items[:10]):
        logger.info(f"  {idx+1}. {item.get('name')} | {item.get('packsize')} | ₹{item.get('price')}")
    return {"status": "ok", "received": count}


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "live-quick-commerce-aggregator",
        "version": "4.0.0"
    }


@app.get("/download/apk")
def download_apk():
    import os
    from fastapi.responses import FileResponse
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quick_compare_app", "build", "app", "outputs", "flutter-apk")
    rel_path = os.path.join(base_dir, "app-release.apk")
    dbg_path = os.path.join(base_dir, "app-debug.apk")
    if os.path.exists(rel_path):
        return FileResponse(
            path=rel_path,
            filename="QuickCompare-release.apk",
            media_type="application/vnd.android.package-archive"
        )
    elif os.path.exists(dbg_path):
        return FileResponse(
            path=dbg_path,
            filename="QuickCompare-debug.apk",
            media_type="application/vnd.android.package-archive"
        )
    raise HTTPException(status_code=404, detail="APK file not found on server.")


@app.post("/api/auth/zepto/send-otp")
async def server_zepto_send_otp(payload: AuthPhoneRequest):
    return await request_zepto_otp(payload)


@app.post("/api/auth/zepto/verify-otp")
async def server_zepto_verify_otp(payload: AuthOtpRequest):
    return await validate_zepto_otp(payload)


if __name__ == "__main__":
    import uvicorn
    import os
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    logger.info(f"Starting Uvicorn server on http://{host}:{port} (default: host='0.0.0.0', port=8000)")
    uvicorn.run("server:app", host=host, port=port, reload=True, loop="asyncio")

