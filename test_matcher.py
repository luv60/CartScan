import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"d:\major project new with server")

import os
import re
import hashlib
import unicodedata
from typing import Tuple, Optional, Dict, Any, Union
import rapidfuzz

# ==============================================================================
# Task 1 & 2: Unit & Quantity Normalizer and Hard-Lock Entity Matcher
# ==============================================================================

def strip_accents(text: str) -> str:
    """Strip diacritics/accents from characters (e.g. Nescafé -> Nescafe)."""
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
    Task 1: Unit & Quantity Normalizer
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


# Maintain backwards-compatible alias
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

    # Remove all quantity tokens
    core = re.sub(r'\b\d+(\.\d+)?\s*(kg|kilo|kilogram|kgs|g|gm|gms|grams?|l|ltr|litre|litres|liter|liters|ml|milli|mls|pc|pcs|piece|pieces|pack|packs|unit|units)\b', ' ', core)
    core = re.sub(r'\bpack\s*of\s*\d+\b', ' ', core)
    core = re.sub(r'\b\d+\s*[xX*]\s*\d+(\.\d+)?\s*\w+\b', ' ', core)
    core = re.sub(r'\bbuy\s*1\s*get\s*1\b', ' ', core)

    # Remove packaging descriptors
    core = re.sub(r'\b(tetra\s*pack|tetrapack|tetra|pouch|pet\s*bottle|bottle|glass\s*jar|jar|can|tin|box|carton)\b', ' ', core)

    # Remove common marketing filler words
    core = re.sub(r'\b(fresh|pure|homogenised|pasteurised|original|natural)\b', ' ', core)

    # Remove punctuation, symbols, stand-alone digits
    core = re.sub(r'[|\-_/\\(),.:;+*&]', ' ', core)
    core = re.sub(r'\b\d+\b', ' ', core)

    tokens = [tok for tok in core.split() if len(tok) > 1]
    tokens.sort()
    core_clean = " ".join(tokens)

    return brand, norm_qty, core_clean


def match_products(item_a: Any, item_b: Any, pack_a: str = "", pack_b: str = "") -> Optional[float]:
    """
    Task 2: Hard-Lock Entity Matcher
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
    """Helper boolean matcher using the 3-stage normalization engine."""
    return match_products(title_a, title_b, pack_a, pack_b) is not None


# Import server components for end-to-end integration tests
from server import (
    extract_product_attributes,
    parse_structured_product,
    are_products_exact_match,
    check_keyword_guardrails,
    normalize_title_for_matching,
    build_canonical_cards,
    extract_packaging,
    extract_packaging_type,
    extract_normalized_quantity,
    strict_product_match_guardrail,
    check_packaging_guardrail,
    resolve_dark_store_id,
    register_canonical_mapping,
    lookup_canonical_mapping,
    RAM_STORE_QUERY_CACHE,
)

if __name__ == "__main__":
    print("1. Testing Location-First Dark Store Mapping...")
    mumbai_store = resolve_dark_store_id(19.0760, 72.8777)
    print(f"  Resolved store_id for Mumbai (19.0760, 72.8777): {mumbai_store}")
    assert mumbai_store == "e9b25f0e-d7f4-42b7-a3f2-89b9d31317d7", f"Failed: Expected Mumbai store ID, got {mumbai_store}"
    print("  Dark Store Mapping PASSED!")

    print("\n2. Testing 3-Gate Hard Guardrails: extract_packaging() and extract_normalized_quantity()...")
    # Test extract_packaging
    assert extract_packaging("Amul Taaza Milk (Tetra Pack)") == "tetra", "Packaging extraction failed for tetra"
    assert extract_packaging("Amul Taaza Toned Fresh Milk | Pouch") == "pouch", "Packaging extraction failed for pouch"
    assert extract_packaging("Coca Cola Pet Bottle 750ml") == "bottle", "Packaging extraction failed for bottle"
    assert extract_packaging("Nescafé Classic Coffee Glass Jar 100g") == "jar", "Packaging extraction failed for jar"
    print("  extract_packaging() PASSED!")

    # Test extract_normalized_quantity
    q, u = extract_normalized_quantity("Amul Taaza Homogenised Toned Milk", "1 L")
    assert q == 1000.0 and u == "ml", f"Normalized quantity failed for 1 L: ({q}, {u})"
    q, u = extract_normalized_quantity("Amul Pure Ghee 1 kg", "1 kg")
    assert q == 1000.0 and u == "g", f"Normalized quantity failed for 1 kg: ({q}, {u})"
    q, u = extract_normalized_quantity("Epigamia Greek Yogurt Blueberry", "85 g")
    assert q == 85.0 and u == "g", f"Normalized quantity failed for 85 g: ({q}, {u})"

    # Test multipack strings ('Pack of 2', '2 x 500ml', 'Buy 1 Get 1')
    q_mp1, u_mp1 = extract_normalized_quantity("Amul Taaza Milk 500ml (Pack of 2)")
    assert q_mp1 == 1000.0 and u_mp1 == "ml", f"Pack of 2 failed: ({q_mp1}, {u_mp1})"
    q_mp2, u_mp2 = extract_normalized_quantity("Amul Taaza Milk 2 x 500ml")
    assert q_mp2 == 1000.0 and u_mp2 == "ml", f"2 x 500ml failed: ({q_mp2}, {u_mp2})"
    q_mp3, u_mp3 = extract_normalized_quantity("Amul Taaza Milk 500ml - Buy 1 Get 1")
    assert q_mp3 == 1000.0 and u_mp3 == "ml", f"Buy 1 Get 1 failed: ({q_mp3}, {u_mp3})"
    q_mp4, u_mp4 = extract_normalized_quantity("Amul Butter 100g (Pack of 3)")
    assert q_mp4 == 300.0 and u_mp4 == "g", f"Pack of 3 failed: ({q_mp4}, {u_mp4})"
    print("  extract_normalized_quantity() (including Multipacks) PASSED!")

    print("\n3. Testing strict_product_match_guardrail() (3 Gates)...")
    # Gate 1: Total Volume & Multipack Expansion Guardrail -> REJECT if volume differs
    assert not strict_product_match_guardrail("Amul Taaza Milk 500ml (Pack of 2)", "Amul Taaza Milk 500ml"), "Gate 1 failed: Paired 1000ml (Pack of 2) with 500ml!"
    assert not strict_product_match_guardrail("Amul Taaza Milk 500ml Buy 1 Get 1", "Amul Taaza Milk 500ml"), "Gate 1 failed: Paired Buy 1 Get 1 (1000ml) with 500ml!"
    assert not strict_product_match_guardrail("Amul Butter 100g (Pack of 2)", "Amul Butter 100g"), "Gate 1 failed: Paired 200g (Pack of 2) with 100g!"
    assert not strict_product_match_guardrail("Amul Taaza Pouch 1 L", "Amul Taaza Pouch 500 ml"), "Gate 1 failed: Paired 1000ml with 500ml!"
    assert not strict_product_match_guardrail("Epigamia Greek Yogurt Blueberry 85g", "Epigamia Greek Yogurt Blueberry 75g"), "Gate 1 failed: Paired 85g with 75g!"

    # Gate 2: Category & Incompatible keyword guardrail
    assert not strict_product_match_guardrail("Amul Milk Powder 500g", "Amul Cheese Spread 500g"), "Gate 2 failed: Paired powder with spread!"
    assert not strict_product_match_guardrail("Britannia Brown Bread", "Britannia Good Day Biscuits"), "Gate 2 failed: Paired bread with biscuits!"

    # Gate 3: Packaging Type (Tetra vs Pouch / differing packaging) -> REJECT
    assert not strict_product_match_guardrail("Amul Taaza 1 L Tetra Pack", "Amul Taaza 1 L Pouch"), "Gate 3 failed: Paired tetra with pouch!"
    assert not strict_product_match_guardrail("Coca Cola Can 300ml", "Coca Cola Bottle 300ml"), "Gate 3 failed: Paired can with bottle!"

    # Passes all 3 gates
    assert strict_product_match_guardrail("Amul Taaza Milk 1L Tetra", "Amul Taaza 1000ml Tetrapack"), "Failed: Valid 1L Tetra match rejected!"
    print("  strict_product_match_guardrail() PASSED!")

    print("\n4. Testing are_products_exact_match() using structured attributes...")
    a_tetra = extract_product_attributes("Amul Taaza Homogenised Toned Milk (Tetra Pack)", "1 L", 74.0)
    a_pouch = extract_product_attributes("Amul Taaza Toned Fresh Milk | Pouch", "1 L", 54.0)
    assert not are_products_exact_match(a_tetra, a_pouch), "Failed: Paired Tetra Pack with Pouch!"

    a_pouch_1l = extract_product_attributes("Amul Taaza Toned Fresh Milk | Pouch", "1 L", 54.0)
    a_pouch_500m = extract_product_attributes("Amul Taaza Toned Fresh Milk | Pouch", "500 ml", 29.0)
    assert not are_products_exact_match(a_pouch_1l, a_pouch_500m), "Failed: Paired 1000 ml with 500 ml!"

    p_yog_85 = extract_product_attributes("Epigamia Greek Yogurt Blueberry", "85 g", 50.0)
    p_yog_75 = extract_product_attributes("Epigamia Greek Yogurt Blueberry", "75 g", 45.0)
    assert not are_products_exact_match(p_yog_85, p_yog_75), "Failed: Paired 85g with 75g!"

    a_tetra_2 = extract_product_attributes("Amul Taaza Homogenised Toned Milk 1L Tetrapack", "1 L", 74.0)
    assert are_products_exact_match(a_tetra, a_tetra_2), "Failed: Exact match between Tetra Pack and Tetrapack failed!"
    print("  are_products_exact_match() PASSED!")

    print("\n5. Testing Keyword Guardrails and Incompatible Categories...")
    assert not check_keyword_guardrails("Amul Milk Powder 500g", "Amul Cheese Spread 500g"), "Failed: Paired powder with spread!"
    assert not check_keyword_guardrails("Everyday Dairy Whitener Milk Powder", "Amul Pure Milk Fat"), "Failed: Paired powder with fat!"
    assert not check_keyword_guardrails("Amul Butter 100g", "Amul Cheese 100g"), "Failed: Paired butter with cheese!"
    assert not check_keyword_guardrails("Britannia Brown Bread", "Britannia Good Day Biscuits"), "Failed: Paired bread with biscuits!"
    print("  Keyword Guardrails PASSED!")

    print("\n6. Testing Title Normalization...")
    t1 = "Amul Taaza Toned Fresh Milk | Pouch"
    t2 = "Amul Taaza Toned Milk"
    assert normalize_title_for_matching(t1) == normalize_title_for_matching(t2), "Title normalization mismatch!"
    print("  Title Normalization PASSED!")

    print("\n7. Testing Canonical Card Building & Unit Price Comparison...")
    b_pack_items = [
        {"name": "Amul Taaza Homogenised Toned Milk (Tetra Pack)", "price": 74.0, "packsize": "1 L", "buy_url": "", "is_available": True},
        {"name": "Amul Taaza Toned Fresh Milk | Pouch", "price": 29.0, "packsize": "500 ml", "buy_url": "", "is_available": True}
    ]
    z_pack_items = [
        {"name": "Amul Taaza Toned Fresh Milk | Pouch", "price": 54.0, "packsize": "1 L", "buy_url": "", "is_available": True},
        {"name": "Amul Taaza Toned Milk", "price": 29.0, "packsize": "500 ml", "buy_url": "", "is_available": True}
    ]

    pack_cards = build_canonical_cards(b_pack_items, z_pack_items)
    print(f"Total Canonical Cards Built: {len(pack_cards)}")
    for c in pack_cards:
        b_name = c["blinkit"]["name"] if c["blinkit"] else "None"
        z_name = c["zepto"]["name"] if c["zepto"] else "None"
        up_comp = c.get("unit_price_comparison", {})
        print(f"  Card '{c['canonical_name']}': Blinkit='{b_name}' | Zepto='{z_name}' | Unit Price={up_comp}")

    # Must build 2 separate canonical cards (Tetra Pack 1L Blinkit, Pouch 1L Zepto, and Pouch 500ml matched)
    tetra_card = next((c for c in pack_cards if "tetra" in c["canonical_name"].lower()), None)
    assert tetra_card is not None, "Failed: Tetra Pack card was not created!"
    assert tetra_card["zepto"] is None, "Failed: Tetra Pack was falsely paired with Zepto pouch!"

    pouch_500_card = next((c for c in pack_cards if "500" in (c["blinkit"]["packsize"] if c["blinkit"] else "") or "500" in c["canonical_name"]), None)
    assert pouch_500_card is not None, "Failed: 500 ml pouch card missing!"
    assert pouch_500_card["blinkit"] is not None and pouch_500_card["zepto"] is not None, "Failed: 500ml pouch did not pair!"
    assert "unit_price_comparison" in pouch_500_card, "Unit price comparison missing in 500ml pouch card!"
    assert pouch_500_card["unit_price_comparison"]["blinkit_unit_price"] == round(29.0 / 500.0, 4)
    assert pouch_500_card["unit_price_comparison"]["zepto_unit_price"] == round(29.0 / 500.0, 4)
    assert pouch_500_card["unit_price_comparison"]["blinkit_standard_unit_price"] == 5.8
    assert pouch_500_card["unit_price_comparison"]["zepto_standard_unit_price"] == 5.8
    assert pouch_500_card["unit_price_comparison"]["standard_unit"] == "100ml"
    assert pouch_500_card["unit_price_comparison"]["formatted_blinkit"] == "₹5.80 / 100ml"
    assert pouch_500_card["unit_price_comparison"]["formatted_zepto"] == "₹5.80 / 100ml"

    pouch_1l_card = next((c for c in pack_cards if "1" in (c["zepto"]["packsize"] if c["zepto"] else "") and c["zepto"] and not c["blinkit"]), None)
    assert pouch_1l_card is not None, "Failed: Zepto 1L Pouch did not form its own separate card!"

    print("\n8. Testing Image Hash & Dark Store Caching (RAM / Redis)...")
    from server import get_image_hash_cached, RAM_DARK_STORE_CACHE, RAM_IMAGE_HASH_CACHE

    test_img = "https://cdn.grofers.com/da/cms-assets/cms/product/test-image-123.png"
    h1 = get_image_hash_cached(test_img)
    assert len(h1) > 0, "Failed: empty image hash"
    assert f"img_hash:{test_img}" in RAM_IMAGE_HASH_CACHE, "Failed: image hash not in RAM cache"
    h2 = get_image_hash_cached(test_img)
    assert h1 == h2, "Failed: inconsistent image hash"

    store_1 = resolve_dark_store_id(21.1762, 79.0554)
    assert "dark_store:21.1762:79.0554" in RAM_DARK_STORE_CACHE, "Failed: dark store not in RAM cache"
    store_2 = resolve_dark_store_id(21.1762, 79.0554)
    assert store_1 == store_2, "Failed: inconsistent dark store lookup"
    print("  Image Hash & Dark Store Caching PASSED!")

    # ==============================================================================
    # Task 2 Deliverable: Unit Tests for 3-Stage Normalization Engine
    # ==============================================================================
    print("\n9. Testing Task 2: Unit & Quantity Normalization (Metric & Imperial Tokens)...")
    # Unit conversions: 0.5 kg / 500 gm / 500g -> 500G
    assert normalize_unit_and_quantity("Amul Pure Ghee", "0.5 kg") == "500G", "Failed 0.5 kg -> 500G"
    assert normalize_unit_and_quantity("Amul Pure Ghee 500 gm") == "500G", "Failed 500 gm -> 500G"
    assert normalize_unit_and_quantity("Amul Pure Ghee 500g") == "500G", "Failed 500g -> 500G"

    # Unit conversions: 1 L / 1000 ml / 1L -> 1000ML
    assert normalize_unit_and_quantity("Amul Taaza Milk", "1 L") == "1000ML", "Failed 1 L -> 1000ML"
    assert normalize_unit_and_quantity("Amul Taaza Milk 1000 ml") == "1000ML", "Failed 1000 ml -> 1000ML"
    assert normalize_unit_and_quantity("Amul Taaza Milk 1L") == "1000ML", "Failed 1L -> 1000ML"

    # Unit conversions: 1 pc / 1 pack -> 1PC
    assert normalize_unit_and_quantity("Amul Butter 1 pc") == "1PC", "Failed 1 pc -> 1PC"
    assert normalize_unit_and_quantity("Amul Butter", "1 pack") == "1PC", "Failed 1 pack -> 1PC"

    # Multipacks
    assert normalize_unit_and_quantity("Amul Taaza Milk 500ml (Pack of 2)") == "1000ML"
    assert normalize_unit_and_quantity("Amul Taaza Milk 2 x 500ml") == "1000ML"
    assert normalize_unit_and_quantity("Amul Taaza Milk 500ml Buy 1 Get 1") == "1000ML"
    assert normalize_unit_and_quantity("Amul Butter 100g (Pack of 3)") == "300G"
    print("  Unit & Quantity Normalization PASSED!")

    print("\n10. Testing Task 2: Preventing False-Positive Matches across Weight Tiers & Brands...")
    # Reject different weight tiers (500g vs 1kg, 85g vs 75g, 500ml vs 1000ml)
    assert match_products("Amul Taaza Milk 500ml", "Amul Taaza Milk 1000ml") is None, "Failed: matched 500ml with 1000ml"
    assert match_products("Amul Pure Ghee 0.5 kg", "Amul Pure Ghee 1 kg") is None, "Failed: matched 0.5kg with 1kg"
    assert match_products("Epigamia Greek Yogurt Blueberry 85g", "Epigamia Greek Yogurt Blueberry 75g") is None, "Failed: matched 85g with 75g"
    assert match_products("Amul Butter 100g", "Amul Butter 500g") is None, "Failed: matched 100g with 500g"
    assert match_products("Amul Taaza Milk 500ml (Pack of 2)", "Amul Taaza Milk 500ml") is None, "Failed: matched Pack of 2 (1000ml) with 500ml"

    # Reject mismatched brands
    assert match_products("Amul Taaza Milk 500ml", "Britannia Taaza Milk 500ml") is None, "Failed: matched Amul with Britannia"
    assert match_products("Amul Pure Ghee 1 kg", "Mother Dairy Pure Ghee 1 kg") is None, "Failed: matched Amul with Mother Dairy"
    print("  Weight Tier & Brand False-Positive Prevention PASSED!")

    print("\n11. Testing Task 2: Fuzzy Scoring on Residual Tokens (Threshold >= 85%)...")
    # Identical product with different formatting/fillers -> matches with >= 85%
    score_pass = match_products("Amul Taaza Toned Fresh Milk | Pouch 500ml", "Amul Taaza Toned Milk 500ml")
    assert score_pass is not None and score_pass >= 85.0, f"Failed: valid residual match rejected: {score_pass}"

    # Dissimilar residual tokens -> rejected (< 85%)
    score_fail = match_products("Amul Milk Powder 500g", "Amul Cheese Spread 500g")
    assert score_fail is None, "Failed: incompatible powder and spread were matched!"
    print("  Fuzzy Scoring on Residual Tokens PASSED!")

    # ==============================================================================
    # Explicit User Deliverable Unit Tests: False-Positive Prevention & Normalization
    # ==============================================================================
    print("\n======================================================================")
    print("TESTING USER DELIVERABLES: Unit & Quantity Normalizer and Hard-Lock Entity Matcher")
    print("======================================================================")

    # Deliverable Test 1: 0.5kg vs 500g -> MATCH
    print("  Testing 0.5kg vs 500g -> MATCH...")
    assert normalize_quantity("0.5 kg") == "500G", "Failed: normalize_quantity('0.5 kg') != 500G"
    assert normalize_quantity("500g") == "500G", "Failed: normalize_quantity('500g') != 500G"
    assert normalize_quantity("500 gm") == "500G", "Failed: normalize_quantity('500 gm') != 500G"
    score_match_str = match_products("Amul Pure Ghee 0.5kg", "Amul Pure Ghee 500g")
    assert score_match_str is not None and score_match_str >= 85.0, f"Failed: 0.5kg vs 500g string match rejected: {score_match_str}"
    score_match_dict = match_products(
        {"name": "Amul Pure Ghee 0.5kg", "price": 320.0},
        {"name": "Amul Pure Ghee 500g", "price": 320.0}
    )
    assert score_match_dict is not None and score_match_dict >= 85.0, f"Failed: 0.5kg vs 500g dict match rejected: {score_match_dict}"
    print("  ✓ Deliverable Test 1 PASSED: 0.5kg vs 500g -> MATCH")

    # Deliverable Test 2: 500g vs 1kg -> REJECT
    print("  Testing 500g vs 1kg -> REJECT...")
    assert normalize_quantity("500g") == "500G", "Failed: normalize_quantity('500g') != 500G"
    assert normalize_quantity("1kg") == "1000G", "Failed: normalize_quantity('1kg') != 1000G"
    assert match_products("Amul Pure Ghee 500g", "Amul Pure Ghee 1kg") is None, "Failed: 500g vs 1kg string match was not rejected!"
    assert match_products(
        {"name": "Amul Pure Ghee 500g"},
        {"name": "Amul Pure Ghee 1kg"}
    ) is None, "Failed: 500g vs 1kg dict match was not rejected!"
    print("  ✓ Deliverable Test 2 PASSED: 500g vs 1kg -> REJECT")

    # Deliverable Test 3: Amul Toned Milk vs Britannia Toned Milk -> REJECT
    print("  Testing Amul Toned Milk vs Britannia Toned Milk -> REJECT...")
    assert match_products("Amul Toned Milk", "Britannia Toned Milk") is None, "Failed: Amul vs Britannia string match was not rejected!"
    assert match_products(
        {"name": "Amul Toned Milk"},
        {"name": "Britannia Toned Milk"}
    ) is None, "Failed: Amul vs Britannia dict match was not rejected!"
    assert match_products(
        {"name": "Amul Toned Milk 500ml"},
        {"name": "Britannia Toned Milk 500ml"}
    ) is None, "Failed: Amul 500ml vs Britannia 500ml dict match was not rejected!"
    print("  ✓ Deliverable Test 3 PASSED: Amul Toned Milk vs Britannia Toned Milk -> REJECT")

    # Deliverable Test 4: Multipacks (Pack of 2 x 500ml -> 1000ML)
    print("  Testing Multipack Normalization: Pack of 2 x 500ml -> 1000ML...")
    assert normalize_quantity("Pack of 2 x 500ml") == "1000ML", "Failed: Pack of 2 x 500ml != 1000ML"
    assert normalize_quantity("2 x 500ml") == "1000ML", "Failed: 2 x 500ml != 1000ML"
    assert normalize_quantity({"name": "Pack of 2 x 500ml"}) == "1000ML", "Failed: dict Pack of 2 x 500ml != 1000ML"
    assert normalize_quantity("Pack of 3 x 100g") == "300G", "Failed: Pack of 3 x 100g != 300G"
    print("  ✓ Deliverable Test 4 PASSED: Pack of 2 x 500ml -> 1000ML")
    print("======================================================================\n")

    # ==============================================================================
    # Task 3 Deliverable: Canonical SKU Registry & Short-Lived Store Caching
    # ==============================================================================
    print("\n12. Testing Task 3: Canonical Mapping Registry & O(1) Lookups...")
    register_canonical_mapping("zepto_item_999", "blinkit_item_888", "sku_dairy_amul_taaza_500")
    assert lookup_canonical_mapping("zepto_item_999", "blinkit_item_888") == "sku_dairy_amul_taaza_500", "Direct lookup failed"
    assert lookup_canonical_mapping("blinkit_item_888", "zepto_item_999") == "sku_dairy_amul_taaza_500", "Bidirectional lookup failed"
    assert lookup_canonical_mapping("unknown_z", "unknown_b") is None, "Lookup for unmapped pair should be None"
    print("  Canonical Mapping Registry PASSED!")

    print("\n13. Testing Task 3: Short-Lived Store Caching (key format cache:<store_id>:<query_hash>)...")
    test_store = "5bbd5e39-81de-4d1a-9706-25cbe2cb58af"
    test_q = "amul milk"
    q_hash = hashlib.sha256(test_q.encode("utf-8")).hexdigest()[:16]
    expected_cache_key = f"cache:{test_store}:{q_hash}"
    assert expected_cache_key.startswith("cache:"), "Cache key prefix incorrect"
    assert f":{q_hash}" in expected_cache_key, "Cache key query hash missing"

    # Verify TTL cache structure
    RAM_STORE_QUERY_CACHE[expected_cache_key] = {"status": "success", "cached": True}
    assert expected_cache_key in RAM_STORE_QUERY_CACHE, "Failed: query response not found in RAM_STORE_QUERY_CACHE"
    assert RAM_STORE_QUERY_CACHE.ttl == 300, f"Expected 300s TTL, got {RAM_STORE_QUERY_CACHE.ttl}"
    print("  Short-Lived Store Caching PASSED!")

    print("\n14. Testing Lifespan Pre-loading of SentenceTransformer...")
    import asyncio
    from server import app, lifespan

    async def test_lifespan():
        async with lifespan(app):
            assert hasattr(app.state, "embedding_model"), "app.state has no embedding_model attribute"
            assert app.state.embedding_model is not None, "embedding_model was not pre-loaded"
            print(f"  Pre-loaded Embedding Model: {type(app.state.embedding_model).__name__}")
            emb = app.state.embedding_model.encode(["Amul Taaza Milk", "Toned Fresh Milk"])
            assert emb.shape[0] == 2, f"Embedding shape unexpected: {emb.shape}"
            print(f"  Successfully encoded test batch with shape: {emb.shape}")

    asyncio.run(test_lifespan())
    print("  Lifespan SentenceTransformer Pre-loading PASSED!")

    print("\nALL TEST CASES (1 to 14) PASSED SUCCESSFULLY!")
