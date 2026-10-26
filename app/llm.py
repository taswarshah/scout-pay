"""LLM provider abstraction: AWS Bedrock (Converse API) or deterministic mock."""
import json, os

PROMPT = ("You are Scout, a relocation agent. Plan a move from {origin} to {destination} for {people} people "
          "with budget ${budget}. Return ONLY JSON: {{\"summary\": str, \"payments\": [{{\"payee\": str, "
          "\"purpose\": str, \"amount\": number, \"due\": str}}]}}")


def mock_plan(origin, destination, people, budget):
    b = float(budget)
    pays = [
        {"payee": "Movers Co. (deposit)", "purpose": "Mover booking deposit", "amount": round(b * 0.15, 2), "due": "now"},
        {"payee": "Landlord", "purpose": "Rental security deposit", "amount": round(b * 0.35, 2), "due": "before move-in"},
        {"payee": "Utility setup", "purpose": "Internet + power activation", "amount": round(b * 0.05, 2), "due": "move-in week"},
    ]
    return {"summary": f"Move {people} people from {origin} to {destination}: book movers, secure a rental, set up utilities.",
            "payments": pays, "provider": "mock"}


def plan_move(origin, destination, people=1, budget=3000):
    if os.getenv("LLM_PROVIDER", "mock") == "bedrock":
        try:
            import boto3
            c = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
            r = c.converse(modelId=os.environ["BEDROCK_MODEL_ID"], messages=[{"role": "user", "content": [
                {"text": PROMPT.format(origin=origin, destination=destination, people=people, budget=budget)}]}])
            out = json.loads(r["output"]["message"]["content"][0]["text"])
            out["provider"] = "bedrock"
            return out
        except Exception as e:  # fall back so demo never breaks
            p = mock_plan(origin, destination, people, budget)
            p["provider"] = f"mock (bedrock failed: {type(e).__name__})"
            return p
    return mock_plan(origin, destination, people, budget)
