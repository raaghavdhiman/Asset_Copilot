import json, re, sqlite3, sys, pathlib, os

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "sample-data")
DB = sys.argv[2] if len(sys.argv) > 2 else "copilot.db"
if os.path.exists(DB):
    os.remove(DB)
con = sqlite3.connect(DB)
cur = con.cursor()

def jl(path):
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out

def js(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

cur.executescript("""
CREATE TABLE areas(area_id, name, team_id, lat, lon);
CREATE TABLE teams(team_id, name, lead_user_id);
CREATE TABLE users(user_id, name, role, team_id);
CREATE TABLE well_type_rules(well_type, producing_min_delta_c, alarm_max_c, source_file);
CREATE TABLE wells(well_id, name, well_type, area_id, area_name, team_id,
    in_charge_user_id, lat, lon, installed_on, recorded_status, equipment_json);
CREATE TABLE inspections(inspection_id, well_id, inspected_at, inspected_date,
    inspector_user_id, method, ambient_temp_c, wellhead_temp_c, delta_c,
    spill_detected, spill_area_pct, spill_est_volume_bbl, notes,
    well_type, threshold_c, is_producing, downtime_days,
    spill_corrected, real_spill);
CREATE TABLE repairs(maintenance_id, well_id, started_at, started_date, completed_at,
    maintenance_type, equipment_kind, issue, action_taken, technician_user_id,
    contractor, downtime_hours, cost_labor, cost_parts, cost_total,
    triggered_by_inspection_id, report_file);
CREATE TABLE reports(maintenance_id, well_id, path, text, containment_hours,
    root_cause, spill_class);
CREATE TABLE regulator_events(event_id, well_id, date, agency, event_type, protocol_id,
    related_inspection_id, hours_to_report, detail, fine_usd, status);
CREATE TABLE notes(note_id, well_id, created_at, author_user_id, note_type,
    inspection_id, text, valid_from, valid_to);
CREATE TABLE docs(doc, well_type, section, text);
""")

# ---------- fleet files ----------
fleet = ROOT / "fleet"
areas = js(fleet / "areas.json")
area_name = {}
for a in areas["areas"]:
    area_name[a["area_id"]] = a["name"]
    cur.execute("INSERT INTO areas VALUES (?,?,?,?,?)",
                (a["area_id"], a["name"], a["team_id"],
                 a["center"]["lat"], a["center"]["lon"]))
for t in areas.get("teams", []):
    cur.execute("INSERT INTO teams VALUES (?,?,?)",
                (t["team_id"], t["name"], t.get("lead_user_id")))

users = js(fleet / "users.json")
if isinstance(users, dict):
    users = users.get("users", [])
for u in users:
    cur.execute("INSERT INTO users VALUES (?,?,?,?)",
                (u["user_id"], u["name"], u["role"], u["team_id"]))

# ---------- manuals: read the thresholds from the text (nothing hardcoded) ----------
rules = {}
for md in sorted((fleet / "well-types").glob("*.md")):
    wt = md.stem
    text = md.read_text(encoding="utf-8")
    m = re.search(r"at least\s*\**\s*(\d+(?:\.\d+)?)\s*°\s*C\s*above\s*ambient", text, re.I)
    a = re.search(r"at or below\s*\**\s*(\d+(?:\.\d+)?)\s*°\s*C", text, re.I)
    if not m:
        print(f"WARNING: could not find the producing rule in {md.name}. Check it by hand.")
    thr = float(m.group(1)) if m else None
    alarm = float(a.group(1)) if a else None
    rules[wt] = thr
    cur.execute("INSERT INTO well_type_rules VALUES (?,?,?,?)", (wt, thr, alarm, md.name))
print("Producing thresholds:", rules)

# ---------- docs split by heading (for the search part later) ----------
def add_docs(path, doc_name, well_type):
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(?=#{1,4}\s)", text)
    for p in parts:
        p = p.strip()
        if not p:
            continue
        heading = p.splitlines()[0].lstrip("# ").strip()
        cur.execute("INSERT INTO docs VALUES (?,?,?,?)", (doc_name, well_type, heading, p))

for md in (fleet / "well-types").glob("*.md"):
    add_docs(md, md.name, md.stem)
for md in (fleet / "sops").glob("*.md"):
    add_docs(md, md.name, None)

# ---------- wells ----------
assets = ROOT / "assets"
well_dirs = sorted(p for p in assets.iterdir() if p.is_dir())
for wd in well_dirs:
    md = js(wd / "metadata.json")
    wid = md["asset_id"]
    cur.execute("INSERT INTO wells VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (wid, md.get("name"), md.get("well_type"), md.get("area_id"),
                 area_name.get(md.get("area_id")), md.get("team_id"),
                 md.get("in_charge_user_id"), md["location"]["lat"], md["location"]["lon"],
                 md.get("installed_on"), md.get("recorded_status"),
                 json.dumps(md.get("equipment", []))))
    wtype = md.get("well_type")
    thr = rules.get(wtype)

    # notes
    notes = jl(wd / "context.jsonl")
    corrected = set()
    for n in notes:
        cur.execute("INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?)",
                    (n["note_id"], wid, n["created_at"], n["author_user_id"], n["note_type"],
                     n.get("inspection_id"), n["text"], n.get("valid_from"), n.get("valid_to")))
        if n["note_type"] == "correction" and n.get("inspection_id"):
            corrected.add(n["inspection_id"])

    # inspections
    for i in jl(wd / "inspections.jsonl"):
        amb, wh = i.get("ambient_temp_c"), i.get("wellhead_temp_c")
        delta = None if amb is None or wh is None else round(wh - amb, 2)
        if delta is None or thr is None:
            prod, down = None, 0
        else:
            prod = 1 if delta >= thr else 0
            down = 0 if prod else 7
        det = 1 if i.get("spill_detected") else 0
        corr = 1 if (i["inspection_id"] in corrected and det) else 0
        cur.execute("INSERT INTO inspections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i["inspection_id"], wid, i["inspected_at"], i["inspected_at"][:10],
                     i.get("inspector_user_id"), i.get("method"), amb, wh, delta,
                     det, i.get("spill_area_pct"), i.get("spill_est_volume_bbl"),
                     i.get("notes"), wtype, thr, prod, down, corr, 1 if (det and not corr) else 0))

    # repairs + reports
    for r in jl(wd / "maintenance.jsonl"):
        c = r.get("cost_usd") or {}
        cur.execute("INSERT INTO repairs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["maintenance_id"], wid, r["started_at"], r["started_at"][:10],
                     r.get("completed_at"), r.get("maintenance_type"), r.get("equipment_kind"),
                     r.get("issue"), r.get("action_taken"), r.get("technician_user_id"),
                     r.get("contractor"), r.get("downtime_hours"), c.get("labor"),
                     c.get("parts"), c.get("total"),
                     r.get("triggered_by_inspection_id"), r.get("report_file")))
        rf = r.get("report_file")
        if rf:
            p = wd / rf
            if not p.exists():
                p = ROOT / rf
            if not p.exists():
                p = wd / "maintenance-reports" / pathlib.Path(rf).name
            if p.exists():
                text = p.read_text(encoding="utf-8")
                m = re.search(r"contained\s+(?:after\s+)?(?:about\s+)?(\d+(?:\.\d+)?)\s*(?:hours|hrs|h)\b", text, re.I)
                rc = re.search(r"(?ms)^##\s*Root cause\s*\n+(.+?)(?=\n##|\Z)", text)
                sc = re.search(r"\((minor|moderate|major)\s+class\)", text, re.I)
                cur.execute("INSERT INTO reports VALUES (?,?,?,?,?,?,?)",
                            (r["maintenance_id"], wid, str(p), text,
                             float(m.group(1)) if m else None,
                             rc.group(1).strip() if rc else None,
                             sc.group(1).lower() if sc else None))
                add_docs  # (reports are searched through the reports table later)
            else:
                print("WARNING: report not found for", r["maintenance_id"], rf)

    # regulator events
    for e in jl(wd / "regulatory-events.jsonl"):
        cur.execute("INSERT INTO regulator_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (e["event_id"], wid, e["date"], e.get("agency"), e.get("event_type"),
                     e.get("protocol_id"), e.get("related_inspection_id"),
                     e.get("hours_to_report"), e.get("detail"), e.get("fine_usd"), e.get("status")))

con.commit()

# ---------- summary ----------
print("\nLoaded into", DB)
for t in ["wells", "inspections", "repairs", "reports", "regulator_events", "notes", "docs"]:
    print(f"  {t:18}", cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
print("\nReport fields found by text matching:")
print("  containment hours:", cur.execute("SELECT COUNT(*) FROM reports WHERE containment_hours IS NOT NULL").fetchone()[0])
print("  root cause       :", cur.execute("SELECT COUNT(*) FROM reports WHERE root_cause IS NOT NULL").fetchone()[0])
print("  spill class      :", cur.execute("SELECT COUNT(*) FROM reports WHERE spill_class IS NOT NULL").fetchone()[0])