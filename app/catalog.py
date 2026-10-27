"""Small demo catalog of relocation services (prices in USD)."""
SERVICES = [
    {"id": "mov-std", "name": "Movers: standard 1-bedroom booking deposit", "category": "movers", "price": "150.00", "tags": ["movers", "moving", "packing", "truck"]},
    {"id": "mov-intl", "name": "International shipping container deposit", "category": "movers", "price": "650.00", "tags": ["movers", "shipping", "container", "international"]},
    {"id": "dep-rent", "name": "Rental security deposit (furnished studio)", "category": "housing", "price": "900.00", "tags": ["rent", "rental", "apartment", "deposit", "housing", "flat"]},
    {"id": "hold-apt", "name": "Apartment holding fee (refundable)", "category": "housing", "price": "200.00", "tags": ["apartment", "holding", "housing", "rent", "flat"]},
    {"id": "visa-fee", "name": "Work visa application fee", "category": "visa", "price": "120.00", "tags": ["visa", "permit", "immigration", "embassy"]},
    {"id": "visa-help", "name": "Visa document review (consultant)", "category": "visa", "price": "80.00", "tags": ["visa", "documents", "consultant", "immigration"]},
    {"id": "tmp-stay", "name": "Temporary stay: 1 week serviced apartment", "category": "housing", "price": "420.00", "tags": ["hotel", "temporary", "stay", "housing"]},
    {"id": "util-net", "name": "Internet + power activation", "category": "utilities", "price": "60.00", "tags": ["utilities", "internet", "power", "electricity", "wifi"]},
    {"id": "sim-card", "name": "Local SIM with 1 month data", "category": "utilities", "price": "25.00", "tags": ["sim", "phone", "mobile", "data"]},
    {"id": "airport", "name": "Airport pickup", "category": "transport", "price": "45.00", "tags": ["airport", "pickup", "taxi", "transport"]},
]
BY_ID = {s["id"]: s for s in SERVICES}


def search(query="", category=None, max_price=None):
    words = [w for w in query.lower().replace(",", " ").split() if len(w) > 2]
    out = []
    for s in SERVICES:
        if category and s["category"] != category: continue
        if max_price is not None and float(s["price"]) > float(max_price): continue
        hay = (s["name"] + " " + " ".join(s["tags"]) + " " + s["category"]).lower()
        score = sum(1 for w in words if w in hay or w.rstrip("s") in hay)
        if not words or score:
            out.append((score, s))
    out.sort(key=lambda x: -x[0])
    return [s for _, s in out]
