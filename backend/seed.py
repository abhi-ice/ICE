"""Demo-tenant seed: creates ICE Construction tenant + admin user + a federal
project with realistic floors, locations, schedule activities, crews, RFIs,
work items, photos, and one approval request. Idempotent — safe to call
multiple times. Run via POST /api/dev/seed."""
from datetime import datetime, date, timedelta, timezone
from database import get_db
from models import gen_id, utc_now
from auth import hash_password


SEED_TENANT_SLUG = "ice"
SEED_USER_EMAIL = "admin@iceops.com"
SEED_USER_PASSWORD = "Demo123!"


async def seed_demo() -> dict:
    db = get_db()

    # 1. Tenant
    tenant = await db.tenants.find_one({"slug": SEED_TENANT_SLUG}, {"_id": 0})
    if not tenant:
        tenant = {
            "id": gen_id(),
            "legal_name": "ICE Construction Inc.",
            "display_name": "ICE Construction",
            "slug": SEED_TENANT_SLUG,
            "type": "general_contractor",
            "status": "active",
            "cmmc_level": "2",
            "facility_clearance": "none",
            "registered_naics": ["236220", "237310"],
            "data_region": "us_east_commercial",
            "work_item_label": "Field Issue",
            "approval_rules": {},
            "created_at": utc_now().isoformat(),
        }
        await db.tenants.insert_one(tenant.copy())

    # 2. Admin user
    user = await db.users.find_one({"email": SEED_USER_EMAIL}, {"_id": 0})
    if not user:
        user = {
            "id": gen_id(),
            "email": SEED_USER_EMAIL,
            "full_name": "Demo Admin",
            "tenant_id": tenant["id"],
            "role": "admin",
            "password_hash": hash_password(SEED_USER_PASSWORD),
            "created_at": utc_now().isoformat(),
        }
        await db.users.insert_one(user.copy())
        await db.tenant_users.insert_one({
            "id": gen_id(), "user_id": user["id"], "tenant_id": tenant["id"],
            "role": "admin", "status": "active", "created_at": utc_now().isoformat(),
        })

    # 3. Project (idempotent on contract_number)
    project = await db.projects.find_one({"tenant_id": tenant["id"], "contract_number": "GS-11P-24-MKC-0027"}, {"_id": 0})
    if not project:
        project = {
            "id": gen_id(),
            "tenant_id": tenant["id"],
            "name": "GSA Federal Building Renovation — 8th Floor",
            "contract_number": "GS-11P-24-MKC-0027",
            "client_name": "U.S. General Services Administration",
            "owner_name": "GSA Region 6",
            "project_type": "renovation",
            "naics_code": "236220",
            "contract_value_usd": 18_750_000,
            "address_line1": "1500 E. Bannister Rd",
            "city": "Kansas City",
            "state": "MO",
            "postal_code": "64131",
            "start_date": (date.today() - timedelta(days=120)).isoformat(),
            "planned_completion_date": (date.today() + timedelta(days=240)).isoformat(),
            "actual_completion_date": None,
            "status": "active",
            "prevailing_wage_required": True,
            "section_3_required": False,
            "buy_america_required": True,
            "em_385_required": True,
            "contract_type": "lump_sum",
            "created_by": user["id"],
            "created_at": utc_now().isoformat(),
        }
        await db.projects.insert_one(project.copy())

    project_id = project["id"]

    # 4. Floors (only seed if empty)
    floor_count = await db.floors.count_documents({"tenant_id": tenant["id"], "project_id": project_id})
    floor_records = []
    if floor_count == 0:
        for n in [1, 2, 3, 4, 5, 6, 7, 8]:
            f = {
                "id": gen_id(), "tenant_id": tenant["id"], "project_id": project_id,
                "floor_number": n, "floor_name": f"Floor {n}",
                "floor_height_ft": 12.0, "elevation_z_ft": (n - 1) * 12.0,
                "gross_area_sqft": 18000.0,
                "created_at": utc_now().isoformat(),
            }
            floor_records.append(f)
            await db.floors.insert_one(f.copy())
    else:
        floor_records = await db.floors.find({"tenant_id": tenant["id"], "project_id": project_id}, {"_id": 0}).to_list(50)

    # 5. Locations (rooms on floor 8 — focus area) + auto polygons for 2.5D
    loc_count = await db.locations.count_documents({"tenant_id": tenant["id"], "project_id": project_id})
    location_records = []
    if loc_count == 0:
        floor8 = next((f for f in floor_records if f["floor_number"] == 8), floor_records[-1])
        # Auto-generate rectangular polygons on a 6x4 grid for 8th floor
        rooms = [
            ("801", "Director's Suite", 850, 0, 0, 30, 25),
            ("802", "Conference Room A", 420, 30, 0, 18, 22),
            ("803", "Open Office East", 2400, 48, 0, 50, 45),
            ("804", "Open Office West", 2200, 0, 25, 28, 45),
            ("805", "Break Room", 380, 28, 25, 16, 22),
            ("806", "Mechanical Closet", 110, 44, 25, 8, 14),
            ("807", "Electrical Room", 90, 52, 25, 8, 12),
            ("808", "Server Room", 240, 60, 25, 14, 18),
            ("809", "Restroom — North", 220, 74, 25, 12, 18),
            ("810", "Restroom — South", 220, 86, 25, 12, 18),
            ("811", "Corridor — Main", 1100, 0, 47, 98, 8),
            ("812", "Stairwell A", 180, 88, 0, 10, 12),
        ]
        for code, name, sqft, x, y, w, h in rooms:
            poly = {
                "type": "Polygon",
                "coordinates": [[[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]],
            }
            loc = {
                "id": gen_id(), "tenant_id": tenant["id"],
                "project_id": project_id, "floor_id": floor8["id"],
                "name": name, "code": code, "location_type": "room",
                "polygon_geojson": poly,
                "centroid_x": x + w / 2, "centroid_y": y + h / 2,
                "area_sqft": sqft,
                "created_at": utc_now().isoformat(),
            }
            location_records.append(loc)
            await db.locations.insert_one(loc.copy())
    else:
        location_records = await db.locations.find({"tenant_id": tenant["id"], "project_id": project_id}, {"_id": 0}).to_list(200)

    # 6. Crews
    crew_count = await db.crews.count_documents({"tenant_id": tenant["id"]})
    crew_records = []
    if crew_count == 0:
        for code, name, trade, size, rate in [
            ("CREW-DEM", "Demo Crew Alpha", "Demolition", 6, 95.0),
            ("CREW-FRM", "Framers Beta", "Framing/Drywall", 5, 88.0),
            ("CREW-MEP", "MEP Crew Gamma", "MEP", 8, 110.0),
            ("CREW-FIN", "Finish Crew Delta", "Finishes", 4, 82.0),
            ("CREW-ELE", "Electrical Crew Echo", "Electrical", 6, 102.0),
        ]:
            c = {
                "id": gen_id(), "tenant_id": tenant["id"], "crew_code": code,
                "name": name, "trade": trade, "target_size": size,
                "hourly_burdened_cost": rate, "status": "active",
                "home_yard_address": "Kansas City, MO",
                "created_at": utc_now().isoformat(),
            }
            crew_records.append(c)
            await db.crews.insert_one(c.copy())
    else:
        crew_records = await db.crews.find({"tenant_id": tenant["id"]}, {"_id": 0}).to_list(50)

    # 7. Equipment
    if await db.equipment.count_documents({"tenant_id": tenant["id"]}) == 0:
        for tag, type_, mfr, mdl, daily in [
            ("LIFT-001", "Scissor Lift", "Genie", "GS-2632", 220),
            ("LIFT-002", "Boom Lift", "JLG", "600AJ", 540),
            ("WELD-005", "Welder", "Lincoln", "Power MIG 256", 75),
            ("GEN-010", "Generator 20kW", "Caterpillar", "XQ20", 180),
            ("FORK-002", "Forklift", "Toyota", "8FGU25", 260),
        ]:
            await db.equipment.insert_one({
                "id": gen_id(), "tenant_id": tenant["id"], "asset_tag": tag,
                "type": type_, "manufacturer": mfr, "model": mdl, "year": 2022,
                "ownership": "owned", "daily_rate": daily, "status": "available",
                "created_at": utc_now().isoformat(),
            })

    # 8. People
    if await db.people.count_documents({"tenant_id": tenant["id"]}) == 0:
        for fn, ln, role, trade, rate, union in [
            ("Marcus", "Reyes", "Superintendent", None, 65.0, None),
            ("Linda", "Chen", "Project Engineer", None, 52.0, None),
            ("Tom", "Brennan", "Foreman", "Demolition", 48.0, "Local 562"),
            ("Aisha", "Patel", "Foreman", "Electrical", 58.0, "IBEW 124"),
            ("Diego", "Ruiz", "Carpenter", "Framing/Drywall", 42.0, "Local 1310"),
            ("Sarah", "Whitfield", "Plumber", "MEP", 51.0, "UA Local 8"),
            ("Khalid", "Hassan", "Painter", "Finishes", 38.0, "DC9"),
            ("Erin", "O'Brien", "Safety Officer", None, 55.0, None),
        ]:
            await db.people.insert_one({
                "id": gen_id(), "tenant_id": tenant["id"],
                "first_name": fn, "last_name": ln, "role": role, "trade": trade,
                "hourly_rate": rate, "union_local": union,
                "employment_type": "ft", "e_verify_status": "verified",
                "i9_on_file": True, "cleared_for_federal": True, "status": "active",
                "created_at": utc_now().isoformat(),
            })

    # 9. Tasks (schedule activities)
    if await db.tasks.count_documents({"tenant_id": tenant["id"], "project_id": project_id}) == 0:
        today = date.today()
        # Build a chain of activities with predecessors. Each row: code, desc, trade, days, offset_days, preds
        activities = [
            ("A1000", "Mobilization & Site Setup", None, 5, -120, [], True),
            ("A1010", "Asbestos Abatement — Floor 8", "Demolition", 10, -110, ["A1000"], False),
            ("A1020", "Demolition — Open Office East (803)", "Demolition", 8, -95, ["A1010"], False),
            ("A1030", "Demolition — Open Office West (804)", "Demolition", 8, -85, ["A1010"], False),
            ("A1040", "Demolition — Director's Suite (801)", "Demolition", 5, -75, ["A1020"], False),
            ("A1050", "Rough Electrical — Floor 8", "Electrical", 14, -65, ["A1020", "A1030"], False),
            ("A1060", "Rough MEP — Floor 8", "MEP", 18, -60, ["A1020", "A1030"], False),
            ("A1070", "Framing — Director's Suite", "Framing/Drywall", 6, -45, ["A1040", "A1050"], False),
            ("A1080", "Framing — Open Offices", "Framing/Drywall", 12, -40, ["A1050", "A1060"], False),
            ("A1090", "Drywall Hang — Director's Suite", "Framing/Drywall", 4, -30, ["A1070"], False),
            ("A1100", "Drywall Hang — Open Offices", "Framing/Drywall", 8, -25, ["A1080"], False),
            ("A1110", "Drywall Finish — Floor 8", "Framing/Drywall", 6, -15, ["A1090", "A1100"], False),
            ("A1120", "Prime & Paint — Floor 8", "Finishes", 8, -8, ["A1110"], False),
            ("A1130", "Ceiling Grid Installation", "Framing/Drywall", 5, -2, ["A1110"], False),
            ("A1140", "Ceiling Tile Drop", "Finishes", 4, 5, ["A1130"], False),
            ("A1150", "Light Fixture Trim", "Electrical", 4, 7, ["A1130"], False),
            ("A1160", "Flooring — Open Offices", "Finishes", 7, 12, ["A1120"], False),
            ("A1170", "Flooring — Director's Suite", "Finishes", 3, 19, ["A1120"], False),
            ("A1180", "Final Plumbing Trim", "MEP", 4, 22, ["A1140"], False),
            ("A1190", "Casework Installation", "Finishes", 6, 25, ["A1170"], False),
            ("A1200", "Final Electrical Devices", "Electrical", 5, 28, ["A1150"], False),
            ("A1210", "Final Cleaning — Floor 8", "Finishes", 3, 35, ["A1190", "A1200"], False),
            ("A1220", "Punch List", "Finishes", 8, 38, ["A1210"], False),
            ("A1230", "Owner Walkthrough", None, 1, 50, ["A1220"], True),
            ("A1240", "Substantial Completion", None, 1, 55, ["A1230"], True),
        ]
        # Distribute locations + crews
        loc_pool = [loc for loc in location_records if loc.get("location_type") == "room"]
        for idx, (code, desc, trade, days, off, preds, is_ms) in enumerate(activities):
            ps = (today + timedelta(days=off)).isoformat()
            pf = (today + timedelta(days=off + days - 1)).isoformat()
            today_iso = today.isoformat()
            done = pf < today_iso
            in_prog = ps <= today_iso <= pf
            pct = 100 if done else (50 if in_prog else 0)
            crew = next((c for c in crew_records if c["trade"] == trade), None) if trade else None
            loc = loc_pool[idx % len(loc_pool)] if loc_pool else None
            await db.tasks.insert_one({
                "id": gen_id(),
                "tenant_id": tenant["id"], "project_id": project_id,
                "activity_code": code, "wbs": f"1.{(idx//5)+1}.{(idx%5)+1}",
                "description": desc, "trade_code": trade,
                "location_id": loc["id"] if loc else None,
                "crew_id": crew["id"] if crew else None,
                "status": "complete" if done else ("in_progress" if in_prog else "not_started"),
                "planned_start": ps, "planned_finish": pf,
                "actual_start": ps if (done or in_prog) else None,
                "actual_finish": pf if done else None,
                "duration_days": float(days),
                "budgeted_hours": float(days) * 8 * (crew["target_size"] if crew else 1),
                "actual_hours": float(days) * 8 * (crew["target_size"] if crew else 1) * pct / 100,
                "pct_complete": pct, "prev_pct_complete": pct,
                "is_milestone": is_ms,
                "is_critical_path": False, "total_float_days": None,
                "lookahead_status": "in_window" if in_prog or (0 <= off <= 14) else "not_in_window",
                "predecessors": preds,
                "source": "master_schedule",
                "created_at": utc_now().isoformat(),
            })

    # 10. RFIs
    if await db.rfis.count_documents({"tenant_id": tenant["id"], "project_id": project_id}) == 0:
        for i, (subj, desc, status_, prio) in enumerate([
            ("Conflict between MEP riser and structural beam at column line C-4",
             "Field condition shows conflict — need engineer direction on rerouting riser or modifying beam.",
             "in_review", "high"),
            ("Spec section 09 21 16 vs drawing A-801 ceiling height discrepancy",
             "Spec calls for 9'-0\" AFF ceiling, drawing shows 8'-6\". Please confirm.",
             "sent", "normal"),
            ("Substitution request — IPS-6 vs spec'd panel system",
             "Owner-approved substitution requested for cost savings of approximately $12K.",
             "answered", "low"),
        ]):
            await db.rfis.insert_one({
                "id": gen_id(), "tenant_id": tenant["id"], "project_id": project_id,
                "rfi_number": f"RFI-{i+1:04d}",
                "subject": subj, "description": desc,
                "to_party": "GSA Region 6 / Architect of Record",
                "from_party": "ICE Construction",
                "schedule_activity_id": None, "location_id": None,
                "spec_section_refs": [], "drawing_refs": [],
                "date_initiated": (date.today() - timedelta(days=20 - i*7)).isoformat(),
                "date_required": (date.today() + timedelta(days=10 - i*3)).isoformat(),
                "date_responded": (date.today() - timedelta(days=2)).isoformat() if status_ == "answered" else None,
                "response_text": "Approved as proposed." if status_ == "answered" else None,
                "status": status_, "priority": prio,
                "cost_impact_estimated_usd": 12000.0 if i == 2 else None,
                "time_impact_estimated_days": 0,
                "created_by": user["id"], "created_at": utc_now().isoformat(),
            })

    # 11. Work items
    if await db.work_items.count_documents({"tenant_id": tenant["id"], "project_id": project_id}) == 0:
        for title, desc, status_, prio in [
            ("Concealed condition — water damage in 803 ceiling",
             "Discovered active water staining in ceiling tile of room 803 during demo. Suspect prior leak from floor above.",
             "investigating", "high"),
            ("Door frame 805 out of plumb",
             "Door frame at break room 805 measured 1/2\" out of plumb after framing.",
             "open", "normal"),
        ]:
            loc = next((loc_ for loc_ in location_records if loc_["code"] == "803" or loc_["code"] == "805"), location_records[0] if location_records else None)
            await db.work_items.insert_one({
                "id": gen_id(), "tenant_id": tenant["id"], "project_id": project_id,
                "location_id": loc["id"] if loc else None,
                "title": title, "description": desc,
                "status": status_, "priority": prio, "root_cause": None,
                "estimated_cost_impact": 4500.0, "actual_cost_impact": None,
                "estimated_time_impact_days": 2.0, "actual_time_impact_days": None,
                "opened_by": user["id"], "opened_at": utc_now().isoformat(),
                "owned_by": user["id"], "target_resolution_date": (date.today() + timedelta(days=7)).isoformat(),
                "closed_at": None, "closure_summary": None,
                "created_at": utc_now().isoformat(),
            })

    # 12. Approvals (one pending)
    if await db.approvals.count_documents({"tenant_id": tenant["id"]}) == 0:
        await db.approvals.insert_one({
            "id": gen_id(), "tenant_id": tenant["id"], "project_id": project_id,
            "kind": "schedule_change",
            "subject_entity_type": "task", "subject_entity_id": "A1080",
            "proposed_payload": {"shift_planned_finish_days": 5, "reason": "MEP rough-in delay due to RFI-0001"},
            "proposed_by": user["id"], "proposed_at": utc_now().isoformat(),
            "required_approvers": [user["id"]], "quorum_required": 1,
            "status": "pending", "decisions": [],
            "final_decided_at": None, "final_decided_by": None,
            "notes": "Pending PM review",
            "created_at": utc_now().isoformat(),
        })

    return {
        "tenant_id": tenant["id"],
        "user_email": SEED_USER_EMAIL,
        "user_password": SEED_USER_PASSWORD,
        "project_id": project_id,
        "message": "Demo tenant seeded.",
    }
