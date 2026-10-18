"""Verify QuickCompare dependencies; --deep also exercises all three models."""
import argparse
import importlib
import json
import os
import sys
from urllib.request import urlopen

cache_root = os.getenv("LOCALAPPDATA", os.path.expanduser("~/.cache"))
for key, folder in (("PLAYWRIGHT_BROWSERS_PATH", "ms-playwright"),
                    ("HF_HOME", "huggingface"), ("TORCH_HOME", "torch")):
    os.environ.setdefault(key, os.path.join(cache_root, folder))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deep", action="store_true")
    args = parser.parse_args()
    failures = []

    def check(name, action):
        try:
            detail = action()
            print(f"PASS {name}" + (f": {detail}" if detail else ""), flush=True)
        except Exception as exc:
            failures.append(name)
            print(f"FAIL {name}: {type(exc).__name__}: {exc}", flush=True)

    print(f"Python: {sys.executable}")
    print(f"Model cache: {os.environ['HF_HOME']}")
    for module in ("cachetools", "curl_cffi", "fastapi", "httpx", "joblib",
                   "numpy", "PIL", "playwright", "pydantic", "rapidfuzz",
                   "redis", "seleniumbase", "sentence_transformers", "sklearn",
                   "starlette", "thefuzz", "torch", "uvicorn"):
        check(module, lambda module=module: importlib.import_module(module).__name__)

    def browser():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            instance = playwright.chromium.launch(headless=True)
            try:
                return instance.version
            finally:
                instance.close()

    def redis_check():
        import redis
        with redis.Redis(host="127.0.0.1", port=6379, socket_timeout=3) as client:
            if not client.ping():
                raise RuntimeError("PING failed")
        return "PONG on localhost:6379"

    def ollama_check():
        base = os.getenv("QUICKCOMPARE_OLLAMA_URL", "http://localhost:11434/api/chat")
        with urlopen(base.rsplit("/", 1)[0] + "/tags", timeout=5) as response:
            installed = json.load(response)
        model = os.getenv("QUICKCOMPARE_QWEN_MODEL", "qwen3:8b")
        if model not in {entry["name"] for entry in installed.get("models", [])}:
            raise RuntimeError(f"Missing model: {model}; run ollama pull {model}")
        return model

    def artifacts():
        from quickcompare_v9_live import _find_root
        import joblib
        root = _find_root()
        artifact = joblib.load(root / "v8_reranker_model.joblib")
        if artifact["model"].n_features_in_ != len(artifact["feature_names"]):
            raise RuntimeError("V8 feature schema mismatch")
        return str(root)

    check("Chromium launch", browser)
    check("Redis service", redis_check)
    check("Ollama model", ollama_check)
    check("V8 matcher artifact", artifacts)

    if args.deep:
        def embedding(name):
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(name, local_files_only=True)
            return str(model.encode(["Amul milk 500 ml"]).shape)

        for name in ("BAAI/bge-small-en-v1.5", "sentence-transformers/all-MiniLM-L6-v2"):
            check(name, lambda name=name: embedding(name))

        def qwen():
            from quickcompare_v9_live import _ollama_verify
            result = _ollama_verify(
                os.getenv("QUICKCOMPARE_QWEN_MODEL", "qwen3:8b"),
                {"a": {"name": "Amul Taaza Milk", "packsize": "500 ml"},
                 "b": {"name": "Amul Taaza Milk", "packsize": "500 ml"}},
                os.getenv("QUICKCOMPARE_OLLAMA_URL", "http://localhost:11434/api/chat"),
                int(os.getenv("QUICKCOMPARE_QWEN_TIMEOUT", "45")),
            )
            if result["decision"] != "MATCH":
                raise RuntimeError(f"Expected MATCH for identical products; got {result}")
            return json.dumps(result)

        check("Qwen product verification", qwen)

    print(f"\n{len(failures)} failed checks.")
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
