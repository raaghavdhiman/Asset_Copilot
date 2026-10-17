import sqlite3, json
from datetime import date, timedelta
import memory

DB = "copilot.db"
TODAY = "2027-01-15"  # "today" from the task. Change here if the test set says otherwise.

# These come from the Spill Response SOP. TODO: parse them from the SOP text
# like we did for the temperature rule, so a changed SOP in the test set still works.
SR01_HOURS = 24      # report to regulator within 24 h
SR01_MIN_BBL = 5.0   # spills of 5 bbl or more are reportable
SR02_HOURS = 4       # containment limit
SR03_DAYS = 60       # SR-03 verification deadline, TODO parse from SOP


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    memory.attach(con)
    memory.build_views(con)      # v_insp now applies corrections
    return con

def days_ago(n):
    return (date.fromisoformat(TODAY) - timedelta(days=n)).isoformat()

def resolve_scope(con, scope):
    """'WELL-0001', 'Northeast Block 8', 'NE-08', 'T200' or 'all' -> list of well ids"""
    s = scope.strip()
    if s.lower() in ("all", "field", "*"):
        q, p = "SELECT well_id FROM wells", ()
    elif s.upper().startswith("WELL-"):
        q, p = "SELECT well_id FROM wells WHERE well_id=?", (s.upper(),)
    elif s.upper() in ("T200", "T350") or s.upper().startswith("T"):
        q, p = "SELECT well_id FROM wells WHERE well_type=?", (s.upper(),)
    else:
        q = "SELECT well_id FROM wells WHERE lower(area_name)=lower(?) OR lower(area_id)=lower(?)"
        p = (s, s)
    return [r[0] for r in con.execute(q, p)]

def ph(ids):
    return ",".join("?" * len(ids))

def rows(cur):
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------- Q1
def well_status(well_id):
    con = db()
    w = con.execute("SELECT * FROM wells WHERE well_id=?", (well_id,)).fetchone()
    if not w:
        return {"error": f"{well_id} not found"}
    i = con.execute("""SELECT i.*, u.name AS inspector FROM v_insp i
        LEFT JOIN users u ON u.user_id=i.inspector_user_id
        WHERE i.well_id=? ORDER BY i.inspected_at DESC LIMIT 1""", (well_id,)).fetchone()
    notes = rows(con.execute("""SELECT n.note_id, n.created_at, u.name AS author, n.note_type,
        n.inspection_id, n.text, n.valid_from, n.valid_to FROM notes n
        LEFT JOIN users u ON u.user_id=n.author_user_id
        WHERE n.well_id=? ORDER BY n.created_at DESC LIMIT 6""", (well_id,)))
    open_repairs = rows(con.execute("""SELECT maintenance_id, started_at, completed_at, issue, equipment_kind
        FROM repairs WHERE well_id=? AND date(completed_at) >= ? ORDER BY started_at""",
        (well_id, TODAY)))
    shared = memory.shared_notes_for(con, well_id)
    corr = memory.corrections_for(con, well_id)
    prod = i["is_producing"]
    derived = "active" if prod else "down"
    cites = ([i["inspection_id"]] + [n["note_id"] for n in notes] + [r["maintenance_id"] for r in open_repairs]
             + [f"NOTE-{n['id']}" for n in shared] + [f"CORR-{c['id']}" for c in corr])
    return {"well_id": well_id, "type": w["well_type"], "area": w["area_name"],
            "producing_now": bool(prod), "threshold_c": i["threshold_c"],
            "latest_inspection": {"id": i["inspection_id"], "at": i["inspected_at"],
                "by": i["inspector"], "method": i["method"], "ambient_c": i["ambient_temp_c"],
                "wellhead_c": i["wellhead_temp_c"], "above_air_c": i["delta_c"],
                "original_wellhead_c": i["orig_wellhead_temp_c"],
                "value_corrected": i["orig_wellhead_temp_c"] != i["wellhead_temp_c"],
                "inspector_note": i["notes"]},
            "recorded_status": w["recorded_status"],
            "recorded_status_out_of_date": w["recorded_status"] != derived,
            "recent_notes": notes, "shared_notes": shared, "corrections": corr,
            "open_repairs": open_repairs, "citations": cites}


# ---------------------------------------------------------------- Q2
def not_producing_now(scope):
    con = db()
    ids = resolve_scope(con, scope)
    out, cites = [], []
    latest = rows(con.execute(f"""SELECT i.*, w.recorded_status, w.area_name FROM v_insp i
        JOIN wells w ON w.well_id=i.well_id
        WHERE i.well_id IN ({ph(ids)}) AND i.is_producing=0
          AND i.inspected_at=(SELECT MAX(inspected_at) FROM v_insp WHERE well_id=i.well_id)
        ORDER BY i.well_id""", ids))
    for r in latest:
        rep = rows(con.execute("""SELECT maintenance_id, started_at, issue FROM repairs
            WHERE well_id=? AND date(completed_at) >= ?""", (r["well_id"], TODAY)))
        nts = rows(con.execute("""SELECT note_id, text FROM notes WHERE well_id=?
            AND note_type!='correction' AND (valid_to IS NULL OR valid_to >= ?)
            ORDER BY created_at DESC LIMIT 3""", (r["well_id"], TODAY)))
        shared = memory.shared_notes_for(con, r["well_id"])
        out.append({"well_id": r["well_id"], "above_air_c": r["delta_c"],
                    "last_inspected": r["inspected_date"], "recorded_status": r["recorded_status"],
                    "open_repairs": rep, "notes": nts, "shared_notes": shared,
                    "inspector_note": r["notes"],
                    "has_recorded_reason": bool(rep or nts or shared)})
        cites += ([r["inspection_id"]] + [x["maintenance_id"] for x in rep]
                  + [x["note_id"] for x in nts] + [f"NOTE-{n['id']}" for n in shared])
    # wells that WOULD be listed from the original data but a correction made them producing
    excluded = rows(con.execute(f"""SELECT i.well_id, i.inspection_id, i.delta_c AS corrected_above_air_c,
        i.orig_wellhead_temp_c, i.wellhead_temp_c AS corrected_wellhead_c, i.orig_ambient_temp_c
        FROM v_insp i WHERE i.well_id IN ({ph(ids)}) AND i.is_producing=1
          AND ROUND(i.orig_wellhead_temp_c-i.orig_ambient_temp_c,2) < i.threshold_c
          AND i.inspected_at=(SELECT MAX(inspected_at) FROM v_insp WHERE well_id=i.well_id)""", ids))
    for e in excluded:
        e["corrections"] = [c for c in memory.corrections_for(con, e["well_id"])
                            if c["scope_value"] == e["inspection_id"] or c["scope_kind"] != "inspection"]
        cites += [e["inspection_id"]] + [f"CORR-{c['id']}" for c in e["corrections"]]
    return {"scope": scope, "wells": out, "excluded_by_correction": excluded, "citations": cites}


# ---------------------------------------------------------------- Q3
def temperature_series(well_id, weeks=13):
    con = db()
    r = rows(con.execute("""SELECT inspection_id, inspected_date, ambient_temp_c, wellhead_temp_c,
        delta_c, is_producing FROM v_insp WHERE well_id=? ORDER BY inspected_at DESC LIMIT ?""",
        (well_id, weeks)))[::-1]
    return {"well_id": well_id, "points": r, "citations": [x["inspection_id"] for x in r]}


# ---------------------------------------------------------------- Q5, Q6
def downtime(scope, start, end, top_n=None):
    con = db()
    ids = resolve_scope(con, scope)
    q = f"""SELECT i.well_id, w.well_type, SUM(i.downtime_days) AS downtime_days,
        SUM(CASE WHEN i.is_producing=0 THEN 1 ELSE 0 END) AS weeks_down
        FROM v_insp i JOIN wells w ON w.well_id=i.well_id
        WHERE i.well_id IN ({ph(ids)}) AND i.inspected_date BETWEEN ? AND ?
        GROUP BY i.well_id ORDER BY downtime_days DESC, i.well_id"""
    res = rows(con.execute(q, ids + [start, end]))
    if top_n:
        res = res[:top_n]
    # evidence: the not-producing inspections behind the numbers.
    # For rankings (top_n) we skip it to keep the result small and save tokens.
    ev = []
    if res and not top_n:
        ev = rows(con.execute(f"""SELECT inspection_id, well_id, inspected_date, ambient_temp_c,
            wellhead_temp_c, delta_c FROM v_insp WHERE well_id IN ({ph([r['well_id'] for r in res])})
            AND is_producing=0 AND inspected_date BETWEEN ? AND ? ORDER BY inspected_at""",
            [r["well_id"] for r in res] + [start, end]))
    return {"scope": scope, "window": [start, end], "wells": res, "evidence": ev,
            "citations": [e["inspection_id"] for e in ev]}


# ---------------------------------------------------------------- Q4, Q7, Q10
def spills(scope, start, end, min_bbl=0, with_containment=False):
    con = db()
    ids = resolve_scope(con, scope)
    res = rows(con.execute(f"""SELECT i.inspection_id, i.well_id, w.well_type, w.area_name,
        w.lat, w.lon, i.inspected_date AS date, i.spill_est_volume_bbl AS barrels,
        i.orig_volume_bbl AS _orig, i.spill_area_pct, i.notes
        FROM v_insp i JOIN wells w ON w.well_id=i.well_id
        WHERE i.well_id IN ({ph(ids)}) AND i.real_spill=1
          AND i.inspected_date BETWEEN ? AND ? AND COALESCE(i.spill_est_volume_bbl,0) >= ?
        ORDER BY i.inspected_at""", ids + [start, end, min_bbl]))
    cites = []
    for s in res:
        orig = s.pop("_orig")
        if orig != s["barrels"]:
            s["volume_corrected"], s["original_bbl"] = True, orig
        cites.append(s["inspection_id"])
        if (s["barrels"] or 0) >= SR01_MIN_BBL:
            ev = con.execute("""SELECT event_id, hours_to_report FROM regulator_events
                WHERE related_inspection_id=? AND event_type='spill_report_filed'""",
                (s["inspection_id"],)).fetchone()
            s["reportable"] = True
            s["regulator_report_on_record"] = bool(ev)
            s["sr01_status"] = ("OVERDUE: no spill report to the regulator is on record" if not ev
                                else f"report {ev['event_id']} filed after {ev['hours_to_report']} h")
            if ev:
                cites.append(ev["event_id"])
        if with_containment:
            rp = con.execute("""SELECT r.maintenance_id, rp.containment_hours, rp.spill_class
                FROM repairs r JOIN reports rp ON rp.maintenance_id=r.maintenance_id
                WHERE r.triggered_by_inspection_id=?""", (s["inspection_id"],)).fetchone()
            s["repair_report"] = rp["maintenance_id"] if rp else None
            s["containment_hours"] = rp["containment_hours"] if rp else None
            s["report_spill_class"] = rp["spill_class"] if rp else None
            s["breaches_sr02"] = bool(rp and rp["containment_hours"] and rp["containment_hours"] > SR02_HOURS)
            if rp:
                cites.append(rp["maintenance_id"])
    total = round(sum(s["barrels"] or 0 for s in res), 1)
    return {"scope": scope, "window": [start, end], "count": len(res),
            "total_bbl": total, "spills": res, "citations": cites}


# ---------------------------------------------------------------- Q8
def spill_heatmap(year):
    con = db()
    res = rows(con.execute("""SELECT w.area_name AS area, CAST(substr(i.inspected_date,6,2) AS INT) AS month,
        COUNT(*) AS spills FROM v_insp i JOIN wells w ON w.well_id=i.well_id
        WHERE i.real_spill=1 AND substr(i.inspected_date,1,4)=?
        GROUP BY w.area_name, month""", (str(year),)))
    return {"year": year, "cells": res}


# ---------------------------------------------------------------- Q9
def rule_check(well_id, start, end):
    con = db()
    breaches, cites = [], []
    sp = spills(well_id, start, end, with_containment=True)["spills"]
    for s in sp:
        cites.append(s["inspection_id"])
        if (s["barrels"] or 0) >= SR01_MIN_BBL:
            ev = con.execute("""SELECT event_id, hours_to_report FROM regulator_events
                WHERE related_inspection_id=? AND event_type='spill_report_filed'""",
                (s["inspection_id"],)).fetchone()
            if ev is None:
                breaches.append({"rule": "SR-01", "inspection": s["inspection_id"],
                    "detail": f"{s['barrels']} bbl is reportable but no spill report is on record (limit {SR01_HOURS} h)"})
            elif ev["hours_to_report"] > SR01_HOURS:
                breaches.append({"rule": "SR-01", "inspection": s["inspection_id"], "event": ev["event_id"],
                    "detail": f"reported after {ev['hours_to_report']} h (limit {SR01_HOURS} h)"})
                cites.append(ev["event_id"])
        if s.get("breaches_sr02"):
            breaches.append({"rule": "SR-02", "inspection": s["inspection_id"], "repair_report": s["repair_report"],
                "detail": f"contained after {s['containment_hours']} h (limit {SR02_HOURS} h)"})
            cites.append(s["repair_report"])
    events = rows(con.execute("""SELECT event_id, date, event_type, protocol_id, related_inspection_id,
        hours_to_report, fine_usd, status, detail FROM regulator_events
        WHERE well_id=? AND date BETWEEN ? AND ? ORDER BY date""", (well_id, start, end)))
    cites += [e["event_id"] for e in events]
    return {"well_id": well_id, "window": [start, end], "computed_breaches": breaches,
            "regulator_events": events,
            "note": "SR-03 (clean-up verification), RC-04 (notice response) and root-cause rules need the SOP text: use search_docs.",
            "citations": sorted(set(cites))}


# ---------------------------------------------------------------- Q11
def repair_costs(well_ids, year):
    con = db()
    ids = [w.upper() for w in well_ids]
    res = rows(con.execute(f"""SELECT well_id, SUM(cost_total) AS total_usd, COUNT(*) AS jobs
        FROM repairs WHERE well_id IN ({ph(ids)}) AND substr(started_date,1,4)=?
        GROUP BY well_id""", ids + [str(year)]))
    return {"year": year, "wells": res,
            "citations": [r["well_id"] + " repairs " + str(year) for r in res]}