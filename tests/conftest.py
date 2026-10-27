import os
os.environ["SCOUT_DB"] = ":memory:"
os.environ.setdefault("LLM_PROVIDER", "mock")
