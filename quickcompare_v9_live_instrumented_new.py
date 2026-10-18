from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen
from typing import Any, Dict, Optional

import joblib


# The deployment matcher files are kept outside server.py so the server stays
# focused on provider fetching/HTTP. Set QUICKCOMPARE_MATCHER_DIR if they live
# outside the current project directory.
SYSTEM_PROMPT = r'''You are a strict semantic product-identity verifier for a grocery price comparison system.

Your job is to decide whether Product A and Product B represent the SAME underlying retail product/variant for comparison purposes.

The upstream V8 probability and V6 signals are evidence only. They are NOT ground truth. You must inspect the product names and structured evidence yourself.

Identity rules:
1. Different brand -> NO_MATCH unless the evidence clearly indicates a marketplace naming artifact rather than a real brand difference.
2. Different underlying product type/category -> NO_MATCH.
3. A clearly different marketed variant is NO_MATCH. Examples include a different flavour, formulation, grain/type, subtype, product line, or model when the text establishes that difference.
4. Quantity and pack count differences alone are MATCH. Different purchasable packaging/container formats alone are normally MATCH.
5. Typos, punctuation, capitalization, word order, marketplace boilerplate, ordinary marketing language, and minor descriptive wording are not identity differences by themselves.
6. A combo containing a genuinely additional consumable/separate product is NO_MATCH versus the base product alone. A minor accessory such as a toothbrush with the same toothpaste does not by itself change the underlying toothpaste identity.
7. Do not infer a different SKU merely because one title contains more words. Determine whether the extra wording identifies a separate marketed product/variant.
8. Do not infer MATCH merely because brand and broad category agree. Check flavour, formulation, subtype, grain/type, product line, and combo content when supplied.
9. Claims such as high fibre, no palm oil, protein, healthy, benefits, percentage claims, etc. are not automatically identity-defining. They matter only when the wording clearly establishes a different marketed formulation/product.
10. Processing/presentation words such as diced, sliced, cut, shaped, etc. are not automatically NO_MATCH. Use NO_MATCH only when the evidence clearly establishes a separately marketed product.
11. If the supplied evidence genuinely cannot distinguish MATCH from NO_MATCH, return REVIEW rather than guessing.
12. Ignore price, retailer/provider, availability, ranking, and location.

Important: do not create product-specific rules from benchmark IDs. Use the same reasoning for every pair.

Return ONLY JSON matching the required schema. No markdown and no chain-of-thought. The reason must be one concise sentence explaining the decisive evidence.'''

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["MATCH", "NO_MATCH", "REVIEW"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["decision", "confidence", "reason"],
    "additionalProperties": False,
}


def _module_path(root: Path, name: str) -> Path:
    return root / name


def _find_root() -> Path:
    candidates = []
    env = os.environ.get("QUICKCOMPARE_MATCHER_DIR")
    if env:
        candidates.append(Path(env).expanduser())
    here = Path(__file__).resolve().parent
    candidates.extend([here, Path.cwd(), here.parent / "quickcompare_matcher_v9_eval"])
    for root in candidates:
        if all((_module_path(root, x)).exists() for x in (
            "product_matcher_v6.py", "product_matcher_v7.py",
            "product_matcher_v8.py", "v8_reranker_model.joblib")):
            return root
    raise FileNotFoundError(
        "V9 matcher artifacts not found. Expected product_matcher_v6.py, "
        "product_matcher_v7.py, product_matcher_v8.py and "
        "v8_reranker_model.joblib. Set QUICKCOMPARE_MATCHER_DIR."
    )


def _load_modules(root: Path):
    import importlib.util
    import sys

    modules = {}
    for name in ("product_matcher_v6", "product_matcher_v7", "product_matcher_v8"):
        path = root / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        modules[name] = module
    return modules["product_matcher_v6"], modules["product_matcher_v7"], modules["product_matcher_v8"]


def _parse_ollama(content: str) -> Dict[str, Any]:
    try:
        data = json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    decision = str(data.get("decision", "REVIEW")).upper().replace(" ", "_").strip()
    if decision not in {"MATCH", "NO_MATCH", "REVIEW"}:
        decision = "REVIEW"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except Exception:
        confidence = 0.0
    return {"decision": decision, "confidence": confidence, "reason": str(data.get("reason", ""))[:1000]}


def _ollama_verify(model: str, payload: Dict[str, Any], url: str, timeout: int) -> Dict[str, Any]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Decide this pair. Return only the required JSON object.\n\n" + json.dumps(payload, ensure_ascii=False)},
        ],
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "options": {"temperature": 0, "num_predict": 180},
    }
    req = Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _parse_ollama(data.get("message", {}).get("content", ""))


class V9LiveMatcher:
    """Live adapter for the validated V7 -> V8 -> V9/Qwen stack.

    It scores only candidate pairs exposed by the exact V7 retriever. V8
    supplies the learned probability. V9 routes >=.90 to MATCH, <=.10 to
    NO_MATCH, and sends the middle band to the local Qwen verifier.
    """

    def __init__(self, catalogs: Dict[str, list]):
        self.root = _find_root()
        self.v6, self.v7, self.v8 = _load_modules(self.root)
        artifact = joblib.load(self.root / "v8_reranker_model.joblib")
        self.model = artifact["model"]
        self.feature_names = artifact["feature_names"]
        expected = getattr(self.model, "n_features_in_", None)
        if expected is not None and expected != len(self.feature_names):
            raise RuntimeError(f"V8 feature schema mismatch: model={expected}, names={len(self.feature_names)}")
        self.retrievers: Dict[str, Any] = {}
        self.products: Dict[str, list] = {}
        self.cache: Dict[str, Optional[Dict[str, Any]]] = {}
        # Retrieval is query-dependent, not pair-dependent. The server may
        # ask us to score the same left product against many cards, so cache
        # the exact V7 candidate union once per (left product, target provider).
        self.retrieval_cache: Dict[str, list] = {}
        self.lock = threading.RLock()
        self.qwen_model = os.environ.get("QUICKCOMPARE_QWEN_MODEL", "qwen3:8b")
        self.ollama_url = os.environ.get("QUICKCOMPARE_OLLAMA_URL", "http://localhost:11434/api/chat")
        self.qwen_timeout = int(os.environ.get("QUICKCOMPARE_QWEN_TIMEOUT", "45"))
        self._stats = {"scores": 0, "cache_hits": 0, "candidate_found": 0, "candidate_miss": 0, "auto_match": 0, "auto_reject": 0, "qwen_review": 0, "qwen_match": 0, "qwen_no_match": 0, "qwen_review_result": 0, "qwen_total_ms": 0.0, "qwen_max_ms": 0.0, "retrieval_total_ms": 0.0, "retrieval_max_ms": 0.0, "score_total_ms": 0.0, "score_max_ms": 0.0}
        self.set_catalogs(catalogs)

    def set_catalogs(self, catalogs: Dict[str, list]) -> None:
        with self.lock:
            self.retrievers.clear()
            self.products.clear()
            for provider, raw_items in catalogs.items():
                normalized = []
                for item in raw_items or []:
                    if not isinstance(item, dict):
                        continue
                    p = dict(item)
                    try:
                        p = self.v7.normalize_for_v7(p, provider=provider)
                    except Exception:
                        # V7 normalization is part of the expected stack. If a
                        # malformed provider item cannot be normalized, leave it
                        # for the existing server-level matcher rather than crash.
                        continue
                    normalized.append(p)
                self.products[provider] = normalized
                try:
                    cls = self.v7.HybridRetriever
                    try:
                        self.retrievers[provider] = cls(normalized, getattr(self.v7, "MODEL_NAME", None))
                    except TypeError:
                        self.retrievers[provider] = cls(normalized)
                except Exception:
                    self.retrievers[provider] = None
            self.cache.clear()
            self.retrieval_cache.clear()
            self._stats = {"scores": 0, "cache_hits": 0, "candidate_found": 0, "candidate_miss": 0, "auto_match": 0, "auto_reject": 0, "qwen_review": 0, "qwen_match": 0, "qwen_no_match": 0, "qwen_review_result": 0, "qwen_total_ms": 0.0, "qwen_max_ms": 0.0, "retrieval_total_ms": 0.0, "retrieval_max_ms": 0.0, "score_total_ms": 0.0, "score_max_ms": 0.0}

    def dump_stats(self):
        s=dict(self._stats)
        print("[V9 STATS] scores=%d cache_hits=%d candidate_found=%d candidate_miss=%d auto_match=%d auto_reject=%d qwen_review=%d qwen_match=%d qwen_no_match=%d qwen_review_result=%d" % tuple(s[k] for k in ("scores","cache_hits","candidate_found","candidate_miss","auto_match","auto_reject","qwen_review","qwen_match","qwen_no_match","qwen_review_result")), flush=True)
        print("[V9 STATS] retrieval_total_ms=%.1f retrieval_max_ms=%.1f qwen_total_ms=%.1f qwen_max_ms=%.1f score_total_ms=%.1f score_max_ms=%.1f" % tuple(s[k] for k in ("retrieval_total_ms","retrieval_max_ms","qwen_total_ms","qwen_max_ms","score_total_ms","score_max_ms")), flush=True)

    @staticmethod
    def _ids(item: Dict[str, Any]) -> set[str]:
        return {str(item[k]).strip() for k in ("id", "product_id", "variant_id") if item.get(k) not in (None, "")}

    def _find_candidate(self, left: Dict[str, Any], right: Dict[str, Any]):
        provider = str(right.get("store") or right.get("provider") or "").lower()
        if provider == "instamart":
            key = "instamart"
        elif "zepto" in provider:
            key = "zepto"
        elif "blinkit" in provider:
            key = "blinkit"
        else:
            key = str(right.get("provider") or "").lower()
        retriever = self.retrievers.get(key)
        products = self.products.get(key, [])
        if retriever is None or not products:
            return None

        # V7 retrieval depends only on the left product and target provider.
        # Previously every pair score repeated the full TF-IDF + embedding
        # retrieval, even when the same left product was compared against
        # multiple cards. Cache that candidate union and keep V8/Qwen pair
        # scoring unchanged.
        left_id = str(
            left.get("product_id")
            or left.get("id")
            or left.get("variant_id")
            or left.get("name")
            or ""
        ).strip().lower()
        retrieval_key = f"{key}|{left_id}"
        with self.lock:
            if retrieval_key in self.retrieval_cache:
                candidates = self.retrieval_cache[retrieval_key]
            else:
                q = self.v6.normalize_product(left, provider=str(left.get("provider") or key))
                t0 = time.perf_counter()
                try:
                    candidates = retriever.retrieve(q, 10, 10, 20)
                except Exception:
                    return None
                dt = (time.perf_counter() - t0) * 1000.0
                self._stats["retrieval_total_ms"] += dt
                self._stats["retrieval_max_ms"] = max(self._stats["retrieval_max_ms"], dt)
                self.retrieval_cache[retrieval_key] = candidates
        right_ids = self._ids(right)
        right_name = str(right.get("name") or right.get("title") or "").strip().lower()
        for pos, c in enumerate(candidates, 1):
            idx = getattr(c, "source_index", None)
            if idx is None or idx < 0 or idx >= len(products):
                continue
            candidate = products[idx]
            if right_ids and (right_ids & self._ids(candidate)):
                return candidate, c, pos
        # ID-less provider records: exact title match is a safe fallback.
        for pos, c in enumerate(candidates, 1):
            idx = getattr(c, "source_index", None)
            if idx is None or idx < 0 or idx >= len(products):
                continue
            candidate = products[idx]
            if right_name and str(candidate.get("name") or "").strip().lower() == right_name:
                return candidate, c, pos
        return None

    def _qwen(self, a: Dict[str, Any], b: Dict[str, Any], v8_probability: float) -> Dict[str, Any]:
        na = self.v6.normalize_product(a, provider=str(a.get("provider") or ""))
        nb = self.v6.normalize_product(b, provider=str(b.get("provider") or ""))
        v6_result = self.v6.compare(na, nb)
        payload = {
            "product_a": a,
            "product_b": b,
            "v8_oof_probability": v8_probability,
            "v6_decision": getattr(v6_result, "decision", ""),
            "v6_reasons": list(getattr(v6_result, "reasons", []) or []),
        }
        t0 = time.perf_counter()
        try:
            result = _ollama_verify(self.qwen_model, payload, self.ollama_url, self.qwen_timeout)
            dt = (time.perf_counter() - t0) * 1000.0
            self._stats["qwen_total_ms"] += dt
            self._stats["qwen_max_ms"] = max(self._stats["qwen_max_ms"], dt)
            d = result.get("decision")
            if d == "MATCH": self._stats["qwen_match"] += 1
            elif d == "NO_MATCH": self._stats["qwen_no_match"] += 1
            else: self._stats["qwen_review_result"] += 1
            print(f"[V9 QWEN] {dt:.0f} ms | decision={d} | A={str(a.get('name') or '')[:80]} | B={str(b.get('name') or '')[:80]}", flush=True)
            return result
        except Exception as exc:
            dt = (time.perf_counter() - t0) * 1000.0
            self._stats["qwen_total_ms"] += dt
            self._stats["qwen_max_ms"] = max(self._stats["qwen_max_ms"], dt)
            print(f"[V9 QWEN] {dt:.0f} ms | ERROR={type(exc).__name__} | A={str(a.get('name') or '')[:80]} | B={str(b.get('name') or '')[:80]}", flush=True)
            # A failed verifier must never silently create a match. Returning
            # REVIEW lets the caller reject the pair rather than merge it.
            return {"decision": "REVIEW", "confidence": 0.0, "reason": f"Qwen unavailable: {type(exc).__name__}"}

    def score(self, a: Dict[str, Any], b: Dict[str, Any]) -> Optional[float]:
        t_score = time.perf_counter()
        self._stats["scores"] += 1
        name_a = str(a.get("name") or "").strip()
        name_b = str(b.get("name") or "").strip()
        if not name_a or not name_b:
            return None
        key_material = "|".join([
            str(a.get("provider") or a.get("store") or ""),
            str(a.get("product_id") or a.get("id") or name_a),
            str(b.get("provider") or b.get("store") or ""),
            str(b.get("product_id") or b.get("id") or name_b),
        ])
        key = hashlib.sha1(key_material.encode("utf-8")).hexdigest()
        with self.lock:
            if key in self.cache:
                self._stats["cache_hits"] += 1
                result = self.cache[key]
                self._stats["score_total_ms"] += (time.perf_counter() - t_score) * 1000.0
                return None if result is None else float(result["score"])

        found = self._find_candidate(a, b)
        if found is None:
            self._stats["candidate_miss"] += 1
            with self.lock:
                self.cache[key] = None
            self._stats["score_total_ms"] += (time.perf_counter() - t_score) * 1000.0
            self._stats["score_max_ms"] = max(self._stats["score_max_ms"], (time.perf_counter() - t_score) * 1000.0)
            return None
        self._stats["candidate_found"] += 1
        candidate, c, union_rank = found
        left = self.v6.normalize_product(a, provider=str(a.get("provider") or ""))
        candidate_for_v8 = dict(candidate)
        candidate_for_v8["_v7_meta"] = {
            "lexical_rank": getattr(c, "lexical_rank", None),
            "embedding_rank": getattr(c, "embedding_rank", None),
            "lexical_score": getattr(c, "lexical_score", 0.0),
            "embedding_score": getattr(c, "embedding_score", 0.0),
            "union_rank": union_rank,
        }
        row = {
            "product_a": a,
            "product_b": b,
            "retrieval_ranks": {"union": union_rank},
        }
        try:
            features, names = self.v8._pair_features(row, candidate_for_v8)
            if len(features) != len(self.feature_names):
                raise RuntimeError(f"V8 feature count mismatch: got {len(features)}, expected {len(self.feature_names)}")
            probability = float(self.model.predict_proba([features])[0][1])
        except Exception:
            with self.lock:
                self.cache[key] = None
            return None

        if probability >= 0.90:
            self._stats["auto_match"] += 1
            result = {"score": probability * 100.0, "decision": "MATCH", "probability": probability}
        elif probability <= 0.10:
            self._stats["auto_reject"] += 1
            result = None
        else:
            self._stats["qwen_review"] += 1
            q = self._qwen(a, b, probability)
            if q["decision"] == "MATCH":
                result = {"score": 72.0 + 28.0 * q["confidence"], "decision": "MATCH", "probability": probability}
            else:
                result = None

        with self.lock:
            self.cache[key] = result
        dt_score = (time.perf_counter() - t_score) * 1000.0
        self._stats["score_total_ms"] += dt_score
        self._stats["score_max_ms"] = max(self._stats["score_max_ms"], dt_score)
        return None if result is None else float(result["score"])


__all__ = ["V9LiveMatcher"]
