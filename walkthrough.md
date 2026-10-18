# Walkthrough: Standardize Unit Pricing Metrics and Multipack Logic

We updated [server.py](file:///d:/major%20project%20new%20with%20server/server.py) to detect multipack strings, compute total normalized volume/weight after multipack expansion, compute standardized unit prices (₹/100g, ₹/100ml, or ₹/unit), and enforce Gate 1 of `strict_product_match_guardrail()` to reject mismatches where total volume differs after multipack expansion.

---

## 1. Multipack Detection & Expansion

In [server.py](file:///d:/major%20project%20new%20with%20server/server.py):
- Updated `extract_normalized_quantity(title: str, packsize: str = "")` and `to_base_metric()` to detect and parse:
  - **Buy X Get Y / BOGO patterns**: e.g., `"Buy 1 Get 1"`, `"Buy 1 Get 1 Free"`, `"BOGO"`, `"B1G1"` (multiplier 2x), `"Buy 2 Get 1"` (multiplier 3x).
  - **Pack / Set / Combo of N**: e.g., `"Pack of 2"`, `"Set of 3"`, `"Combo of 4"`, `"2 pack"`, `"3 pcs"`.
  - **Explicit multipacks**: `"2 x 500ml"`, `"2x500 ml"`, `"3 * 1 L"` and reversed `"500ml x 2"`, `"500g * 3"`.
- Multiplies single volume/mass by total multipack multiplier to yield the exact total expanded volume or mass.
  - `"Amul Taaza Milk 500ml (Pack of 2)"` $\to$ `(1000.0, 'ml')`
  - `"Amul Taaza Milk 2 x 500ml"` $\to$ `(1000.0, 'ml')`
  - `"Amul Taaza Milk 500ml - Buy 1 Get 1"` $\to$ `(1000.0, 'ml')`
  - `"Amul Butter 100g (Pack of 3)"` $\to$ `(300.0, 'g')`
  - `"Coca Cola Can 300ml (Pack of 6)"` $\to$ `(1800.0, 'ml')`
  - `"Avocado", "Pack of 2"` $\to$ `(2.0, 'unit')`

---

## 2. Standardized Unit Pricing Metrics

Added `compute_standard_unit_price(price: float, quantity: float, unit: str) -> Dict[str, Any]` and integrated it into `build_canonical_cards()`:
- **Liquids (`ml`)**: Normalized to **₹/100ml**: `round((price / quantity) * 100.0, 2)`.
- **Solids (`g`)**: Normalized to **₹/100g**: `round((price / quantity) * 100.0, 2)`.
- **Count / Units (`unit`)**: Normalized to **₹/unit**: `round(price / quantity, 2)`.
- Populated into canonical card `unit_price_comparison`:
  - `blinkit_standard_unit_price`: Standard unit price for Blinkit (e.g. `5.8` for 500ml @ ₹29)
  - `zepto_standard_unit_price`: Standard unit price for Zepto (e.g. `5.8` for 500ml @ ₹29)
  - `standard_unit`: `"100ml"`, `"100g"`, or `"unit"`
  - `metric`: `"₹/100ml"`, `"₹/100g"`, or `"₹/unit"`
  - `formatted_blinkit`: `"₹5.80 / 100ml"`
  - `formatted_zepto`: `"₹5.80 / 100ml"`
  - Preserved `blinkit_unit_price` and `zepto_unit_price` for backward compatibility.

---

## 3. Gate 1 Guardrail: Total Volume & Multipack Expansion

Updated `strict_product_match_guardrail()` in [server.py](file:///d:/major%20project%20new%20with%20server/server.py):
- **Gate 1: Total Volume & Multipack Expansion Guardrail**:
  Compares total normalized quantity after multipack expansion (`extract_normalized_quantity`).
  Rejects immediately if relative volume difference exceeds 1%:
  - `"Amul Taaza Milk 500ml (Pack of 2)"` (1000ml) vs `"Amul Taaza Milk 500ml"` (500ml) $\to$ **REJECTED by Gate 1**.
  - `"Amul Taaza Milk 500ml Buy 1 Get 1"` (1000ml) vs `"Amul Taaza Milk 500ml"` (500ml) $\to$ **REJECTED by Gate 1**.
  - `"Amul Butter 100g (Pack of 2)"` (200g) vs `"Amul Butter 100g"` (100g) $\to$ **REJECTED by Gate 1**.
- **Gate 2**: Category & Incompatible Keyword Guardrail (powder vs spread/fat, bread vs biscuits).
- **Gate 3**: Packaging Type Guardrail (tetra vs pouch, can vs bottle).

---

## 4. Verification Results

### A. Full Test Suite (`py test_matcher.py`)
Executed all 9 test sections:
- Location-First Dark Store Mapping: **PASSED**
- 3-Gate Hard Guardrails (`extract_packaging`, `extract_normalized_quantity` with Multipacks): **PASSED**
- `strict_product_match_guardrail` (Gate 1 Total Volume Mismatch Rejection): **PASSED**
- `are_products_exact_match`: **PASSED**
- Keyword Guardrails: **PASSED**
- Title Normalization: **PASSED**
- Canonical Card Building & Standard Unit Price Comparison (₹/100ml, ₹/100g, ₹/unit): **PASSED**
- Image Hash & Dark Store Caching (RAM / Redis): **PASSED**
- Lifespan Pre-loading of SentenceTransformer: **PASSED**

```
ALL TEST CASES PASSED SUCCESSFULLY!
```

### B. Redis Dual-Tier Fallback Tests (`test_server_redis_fallback.py`)
- Lifespan initialization and Redis client timeout handling: **PASSED**
- Synchronous & Asynchronous caching & RAM fallback: **PASSED**
