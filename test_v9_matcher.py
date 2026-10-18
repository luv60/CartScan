import sys
from pathlib import Path

MATCHER_DIR = Path(__file__).parent / "quickcompare_matcher_v9_eval"
sys.path.insert(0, str(MATCHER_DIR))

from product_matcher_v6 import normalize_product, compare
from product_matcher_v7 import HybridRetriever
from product_matcher_v8 import _pair_features

import joblib


print("=" * 70)
print("QUICKCOMPARE V6/V7/V8 SMOKE TEST")
print("=" * 70)

# ---------------------------------------------------------
# 1. V6
# ---------------------------------------------------------

a = {
    "name": "Lay's Classic Salted Potato Chips",
    "brand": "Lay's",
    "quantity": 50,
    "packsize": "50 g",
}

b = {
    "name": "Lays Classic Salted Potato Chips",
    "brand": "Lay's",
    "quantity": 50,
    "packsize": "50 g",
}

na = normalize_product(a, provider="blinkit")
nb = normalize_product(b, provider="zepto")

v6 = compare(na, nb)

print("\n[V6]")
print("Decision :", v6.decision)
print("Score    :", v6.score)
print("Reasons  :", v6.reasons)

# ---------------------------------------------------------
# 2. V7
# ---------------------------------------------------------

catalog = [
    nb,
    normalize_product(
        {
            "name": "Lay's Magic Masala Chips",
            "brand": "Lay's",
            "quantity": 50,
            "packsize": "50 g",
        },
        provider="zepto",
    ),
    normalize_product(
        {
            "name": "Kurkure Masala Munch",
            "brand": "Kurkure",
            "quantity": 90,
            "packsize": "90 g",
        },
        provider="zepto",
    ),
]

print("\n[V7]")
print("Loading HybridRetriever...")

retriever = HybridRetriever(catalog)

candidates = retriever.retrieve(
    na,
    lexical_k=10,
    embedding_k=10,
    union_k=20,
)

print("Candidates:", len(candidates))

for i, c in enumerate(candidates, 1):
    print(
        f"  {i}. index={c.source_index} "
        f"lex_rank={c.lexical_rank} "
        f"emb_rank={c.embedding_rank} "
        f"lex_score={c.lexical_score:.4f} "
        f"emb_score={c.embedding_score:.4f} "
        f"v6={c.v6_decision}"
    )

# ---------------------------------------------------------
# 3. V8 feature generation
# ---------------------------------------------------------

print("\n[V8]")

target = dict(nb)

target["_v7_meta"] = {
    "lexical_rank": candidates[0].lexical_rank,
    "embedding_rank": candidates[0].embedding_rank,
    "lexical_score": candidates[0].lexical_score,
    "embedding_score": candidates[0].embedding_score,
    "union_rank": 1,
}

row = {
    "product_a": a,
    "product_b": b,
    "retrieval_ranks": {
        "lexical": candidates[0].lexical_rank,
        "embedding": candidates[0].embedding_rank,
        "union": 1,
    },
}

features, feature_names = _pair_features(row, target)

print("Feature count:", len(features))
print("Feature names:", len(feature_names))

# ---------------------------------------------------------
# 4. Load V8 model
# ---------------------------------------------------------

model_path = MATCHER_DIR / "v8_reranker_model.joblib"

artifact = joblib.load(model_path)

model = artifact["model"]
saved_feature_names = artifact["feature_names"]

print("\n[MODEL]")
print("Model type:", type(model).__name__)
print("Expected features:", model.n_features_in_)
print("Saved feature names:", len(saved_feature_names))
print("Generated features:", len(features))

if model.n_features_in_ != len(features):
    raise RuntimeError(
        f"Feature mismatch: model expects "
        f"{model.n_features_in_}, generated {len(features)}"
    )

probability = float(model.predict_proba(features.reshape(1, -1))[0, 1])

print("V8 MATCH probability:", probability)

# ---------------------------------------------------------
# 5. V9 routing
# ---------------------------------------------------------

if probability >= 0.90:
    decision = "MATCH"
elif probability <= 0.10:
    decision = "NO_MATCH"
else:
    decision = "AI_REVIEW"

print("\n[V9 ROUTING]")
print("Decision:", decision)

print("\n" + "=" * 70)
print("SMOKE TEST COMPLETE")
print("=" * 70)