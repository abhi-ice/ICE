"""Seed auxiliary data: trade_taxonomy (CSI MasterFormat 2020 division+section
samples) and cost_rate_categories. Idempotent."""
from datetime import datetime, timezone
from database import get_db
from models import gen_id, utc_now


# CSI MasterFormat 2020 — divisions + a sample of common section codes
CSI_DIVISIONS = [
    ("01", "General Requirements"),
    ("02", "Existing Conditions"),
    ("03", "Concrete"),
    ("04", "Masonry"),
    ("05", "Metals"),
    ("06", "Wood, Plastics, and Composites"),
    ("07", "Thermal and Moisture Protection"),
    ("08", "Openings"),
    ("09", "Finishes"),
    ("10", "Specialties"),
    ("11", "Equipment"),
    ("12", "Furnishings"),
    ("13", "Special Construction"),
    ("14", "Conveying Equipment"),
    ("21", "Fire Suppression"),
    ("22", "Plumbing"),
    ("23", "Heating, Ventilating, and Air Conditioning (HVAC)"),
    ("25", "Integrated Automation"),
    ("26", "Electrical"),
    ("27", "Communications"),
    ("28", "Electronic Safety and Security"),
    ("31", "Earthwork"),
    ("32", "Exterior Improvements"),
    ("33", "Utilities"),
]

# Sample cost rates that recovery + change-order agents can reference.
SEED_RATES = [
    ("LAB-CARP", "Carpenter labor",          "hour",  78.0,  102.0, "Framing/Drywall"),
    ("LAB-ELEC", "Electrician labor",        "hour",  92.0,  118.0, "Electrical"),
    ("LAB-PLMB", "Plumber labor",            "hour",  90.0,  115.0, "MEP"),
    ("LAB-PNTR", "Painter labor",            "hour",  62.0,   82.0, "Finishes"),
    ("LAB-DEMO", "Demolition labor",         "hour",  68.0,   88.0, "Demolition"),
    ("LAB-IRON", "Ironworker labor",         "hour",  98.0,  126.0, "Metals"),
    ("LAB-CONC", "Concrete finisher",        "hour",  76.0,   98.0, "Concrete"),
    ("EQ-LIFT",  "Scissor lift, 26ft",       "day",  220.0,  220.0, None),
    ("EQ-BOOM",  "Boom lift, 60ft",          "day",  540.0,  540.0, None),
    ("EQ-FORK",  "Forklift, 5k lb",          "day",  260.0,  260.0, None),
    ("EQ-WELD",  "MIG welder",               "day",   75.0,   75.0, None),
    ("EQ-GEN",   "Generator, 20kW",          "day",  180.0,  180.0, None),
    ("MAT-DRY",  "Drywall, 5/8\" Type X",   "sqft",   1.20,    1.20, "Framing/Drywall"),
    ("MAT-PNT",  "Latex paint, 2-coat",     "sqft",   2.50,    2.50, "Finishes"),
    ("MAT-CONC", "Concrete 4000 PSI",         "cy",  165.0,  165.0, "Concrete"),
    ("MAT-REB",  "Rebar #5",                  "lb",    1.20,    1.20, "Concrete"),
]


async def seed_taxonomy_and_rates(tenant_id: str):
    db = get_db()
    if await db.trade_taxonomy.count_documents({}) == 0:
        for i, (code, name) in enumerate(CSI_DIVISIONS):
            await db.trade_taxonomy.insert_one({
                "id": gen_id(),
                "tenant_id": None,
                "code": code, "name": name,
                "csi_division": code,
                "parent_id": None,
                "sort_order": i,
                "created_at": utc_now().isoformat(),
            })
    if await db.cost_rate_categories.count_documents({"tenant_id": tenant_id}) == 0:
        for code, name, unit, base, pw, trade in SEED_RATES:
            await db.cost_rate_categories.insert_one({
                "id": gen_id(),
                "tenant_id": tenant_id,
                "code": code, "name": name, "unit": unit,
                "base_rate_usd": base, "prevailing_wage_rate_usd": pw,
                "trade_code": trade,
                "notes": None, "effective_start": None, "effective_end": None,
                "created_at": utc_now().isoformat(),
            })
