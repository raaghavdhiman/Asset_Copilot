import json, pathlib, re, sys
from collections import Counter, defaultdict

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "test-data")


# ------------------------------------------------------------ raw loaders
def jl(p):
    if not p.exists():
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]

def thresholds():
    out = {}
    for md in (ROOT / "fleet" / "well-types").glob("*.md"):
        m = re.search(r"at least\s*\**\s*(\d+(?:\.\d+)?)\s*°\s*C\s*above\s*ambient",
                      md.read_text(encoding="utf-8"), re.I)
        out[md.stem] = float(m.group(1))
    return out

THR = thresholds()
AREAS = json.load(open(ROOT / "fleet" / "areas.json", encoding="utf-8"))["areas"]
AREA_OF = {w: a["area_id"] for a in AREAS for w in a["asset_ids"]}
AREA_NAME = {a["area_id"]: a["name"] for a in AREAS}
USERS = {u["user_id"]: u for u in json.load(open(ROOT / "fleet" / "users.json", encoding="utf-8"))
         ["users" if isinstance(json.load(open(ROOT / "fleet" / "users.json", encoding="utf-8")), dict) else 0]
         } if False else None


def wells():
    for d in sorted((ROOT / "assets").iterdir()):
        if d.is_dir():
            yield d.name, d, json.load(open(d / "metadata.json", encoding="utf-8"))

def inspections(d):
    return jl(d / "inspections.jsonl")

def corrected_ids(d):
    """Formal corrections in the dataset's own notes (spill false alarms)."""
    return {n["inspection_id"] for n in jl(d / "context.jsonl")
            if n.get("note_type") == "correction" and n.get("inspection_id")}


# ------------------------------------------------------------ building blocks
def producing(rec, wtype):
    if rec["wellhead_temp_c"] is None or rec["ambient_temp_c"] is None:
        return None
    return round(rec["wellhead_temp_c"] - rec["ambient_temp_c"], 2) >= THR[wtype]

def real_spill(rec, corr):
    return bool(rec.get("spill_detected")) and rec["inspection_id"] not in corr

def in_window(rec, start, end):
    return start <= rec["inspected_at"][:10] <= end


# ------------------------------------------------------------ ready checks
def spills_by_inspector(start="0000", end="9999"):
    c = Counter()
    for w, d, meta in wells():
        corr = corrected_ids(d)
        for r in inspections(d):
            if in_window(r, start, end) and real_spill(r, corr):
                c[r["inspector_user_id"]] += 1
    return c.most_common(5)

def spills_by_area(start, end):
    c = Counter()
    for w, d, meta in wells():
        corr = corrected_ids(d)
        for r in inspections(d):
            if in_window(r, start, end) and real_spill(r, corr):
                c[AREA_NAME[AREA_OF[w]]] += 1
    return c.most_common()

def downtime_days(area_id, start, end, top=5):
    res = []
    for w, d, meta in wells():
        if AREA_OF.get(w) != area_id:
            continue
        n = sum(1 for r in inspections(d) if in_window(r, start, end)
                and producing(r, meta["well_type"]) is False)
        res.append((n * 7, w, meta["well_type"]))
    return sorted(res, key=lambda x: (-x[0], x[1]))[:top]

def repair_cost(well_ids, year):
    out = {}
    for w, d, meta in wells():
        if w in well_ids:
            out[w] = sum((m.get("cost_usd") or {}).get("total", 0) for m in jl(d / "maintenance.jsonl")
                         if m["started_at"][:4] == str(year))
    return out

def corrective_by_contractor():
    c = Counter()
    for w, d, meta in wells():
        for m in jl(d / "maintenance.jsonl"):
            if m["maintenance_type"] == "corrective":
                c[m.get("contractor") or "in-house"] += 1
    return c.most_common(5)

def not_producing_now(area_id):
    out = []
    for w, d, meta in wells():
        if AREA_OF.get(w) != area_id:
            continue
        recs = inspections(d)
        if recs:
            last = max(recs, key=lambda r: r["inspected_at"])
            if producing(last, meta["well_type"]) is False:
                out.append((w, last["inspection_id"], last["inspected_at"][:10]))
    return out

def late_regulator_reports(start="0000", end="9999"):
    out = []
    for w, d, meta in wells():
        for e in jl(d / "regulatory-events.jsonl"):
            if e["event_type"] == "spill_report_filed" and start <= e["date"] <= end \
               and (e.get("hours_to_report") or 0) > 24:
                out.append((w, e["event_id"], e["hours_to_report"]))
    return out


# ------------------------------------------------------------ run from the command line
if __name__ == "__main__":
    cmd = sys.argv[2] if len(sys.argv) > 2 else "help"
    a = sys.argv[3:]
    if cmd == "inspector":          print(spills_by_inspector(*a))
    elif cmd == "spills_area":      print(spills_by_area(*a))
    elif cmd == "downtime":         print(downtime_days(*a))
    elif cmd == "cost":             print(repair_cost(a[0].split(","), a[1]))
    elif cmd == "contractor":       print(corrective_by_contractor())
    elif cmd == "notproducing":     print(not_producing_now(*a))
    elif cmd == "late_reports":     print(late_regulator_reports(*a))
    else:
        print("usage: py prove.py <data-folder> <check> [args]\n"
              " inspector [start end]        spills_area start end      downtime AREA_ID start end\n"
              " cost WELL-1,WELL-2 year      contractor                 notproducing AREA_ID\n"
              " late_reports [start end]")