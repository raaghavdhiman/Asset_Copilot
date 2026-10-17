import sqlite3, re
from datetime import datetime, timezone, date, timedelta

MEM_DB = "memory.db"
RCA_DEFAULT = 3   # manual default (T200 manual section 10). TODO: parse from the manual text.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS {p}corrections(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT,
  author_user_id TEXT, scope_kind TEXT, scope_value TEXT, field TEXT, original_value TEXT,
  new_value TEXT, reason TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS {p}rules(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT,
  author_user_id TEXT, scope_kind TEXT, scope_value TEXT, rule_key TEXT, value TEXT, note TEXT,
  active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS {p}shared_notes(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT,
  author_user_id TEXT, well_id TEXT, text TEXT, claim TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS {p}chat_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
  user_id TEXT, chat_id TEXT, role TEXT, text TEXT, wells TEXT);
"""
ALLOWED_FIELDS = {"wellhead_temp_c", "ambient_temp_c", "spill_est_volume_bbl", "spill_detected"}


def schema(prefix):
    return _SCHEMA.replace("{p}", prefix)

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------- connection + corrected view
def attach(con):
    con.execute(f"ATTACH DATABASE '{MEM_DB}' AS mem")
    con.executescript(schema("mem."))

def _corr(field, cast):
    return f"""COALESCE((SELECT CAST(c.new_value AS {cast}) FROM mem.corrections c
      WHERE c.active=1 AND c.field='{field}' AND (
        (c.scope_kind='inspection' AND c.scope_value=i.inspection_id) OR
        (c.scope_kind='well' AND c.scope_value=i.well_id) OR
        (c.scope_kind='well_type' AND c.scope_value=i.well_type) OR
        c.scope_kind='all')
      ORDER BY CASE c.scope_kind WHEN 'inspection' THEN 1 WHEN 'well' THEN 2
               WHEN 'well_type' THEN 3 ELSE 4 END, c.id DESC LIMIT 1), i.{field})"""

def build_views(con):
    con.execute(f"""CREATE TEMP VIEW v_c AS SELECT i.*,
        {_corr('wellhead_temp_c','REAL')} AS wh_c, {_corr('ambient_temp_c','REAL')} AS amb_c,
        {_corr('spill_est_volume_bbl','REAL')} AS vol_c, {_corr('spill_detected','INTEGER')} AS det_c
        FROM inspections i""")
    con.execute("""CREATE TEMP VIEW v_insp AS SELECT inspection_id, well_id, inspected_at, inspected_date,
        inspector_user_id, method, amb_c AS ambient_temp_c, wh_c AS wellhead_temp_c,
        ROUND(wh_c-amb_c,2) AS delta_c, det_c AS spill_detected, spill_area_pct,
        vol_c AS spill_est_volume_bbl, notes, well_type, threshold_c,
        CASE WHEN wh_c IS NULL OR amb_c IS NULL OR threshold_c IS NULL THEN NULL
             WHEN ROUND(wh_c-amb_c,2)>=threshold_c THEN 1 ELSE 0 END AS is_producing,
        CASE WHEN wh_c IS NULL OR amb_c IS NULL OR threshold_c IS NULL THEN 0
             WHEN ROUND(wh_c-amb_c,2)>=threshold_c THEN 0 ELSE 7 END AS downtime_days,
        spill_corrected,
        CASE WHEN det_c=1 AND spill_corrected=0 THEN 1 ELSE 0 END AS real_spill,
        wellhead_temp_c AS orig_wellhead_temp_c, ambient_temp_c AS orig_ambient_temp_c,
        spill_est_volume_bbl AS orig_volume_bbl
        FROM v_c""")


# ---------------------------------------------------------------- readers used by tools
def shared_notes_for(con, well_id):
    return _rows(con.execute("""SELECT s.id, s.created_at, u.name AS author, s.text, s.claim, s.status
        FROM mem.shared_notes s LEFT JOIN users u ON u.user_id=s.author_user_id
        WHERE s.well_id=? ORDER BY s.id DESC LIMIT 6""", (well_id,)))

def corrections_for(con, well_id):
    r = con.execute("SELECT well_type FROM wells WHERE well_id=?", (well_id,)).fetchone()
    wt = r[0] if r else ""
    num = well_id.split("-")[-1]
    return _rows(con.execute("""SELECT c.id, c.created_at, u.name AS author, c.scope_kind, c.scope_value,
        c.field, c.original_value, c.new_value, c.reason
        FROM mem.corrections c LEFT JOIN users u ON u.user_id=c.author_user_id
        WHERE c.active=1 AND (c.scope_value=? OR c.scope_value LIKE ?
              OR (c.scope_kind='well_type' AND c.scope_value=?) OR c.scope_kind='all')
        ORDER BY c.id DESC LIMIT 8""", (well_id, f"INS-{num}-%", wt)))

def effective_rule(con, key, well_id, well_type):
    return con.execute("""SELECT r.id, r.value, r.scope_kind, r.scope_value, r.created_at, u.name AS author
        FROM mem.rules r LEFT JOIN users u ON u.user_id=r.author_user_id
        WHERE r.active=1 AND r.rule_key=? AND ((r.scope_kind='well' AND r.scope_value=?)
              OR (r.scope_kind='well_type' AND r.scope_value=?) OR r.scope_kind='all')
        ORDER BY CASE r.scope_kind WHEN 'well' THEN 1 WHEN 'well_type' THEN 2 ELSE 3 END, r.id DESC
        LIMIT 1""", (key, well_id, well_type)).fetchone()


# ---------------------------------------------------------------- writers (author = current user)
def save_correction(user_id, field, new_value, reason, inspection_id=None, well_id=None,
                    inspection_date=None, well_type=None):
    import tools as t
    con = t.db()
    if field not in ALLOWED_FIELDS:
        return {"error": f"field must be one of {sorted(ALLOWED_FIELDS)}"}
    try:
        float(new_value)
    except Exception:
        return {"error": "new_value must be a number"}
    if not inspection_id and well_id and inspection_date:
        r = con.execute("SELECT inspection_id FROM inspections WHERE well_id=? AND inspected_date=?",
                        (well_id.upper(), str(inspection_date)[:10])).fetchone()
        if not r:
            return {"error": f"no inspection of {well_id} on {inspection_date}"}
        inspection_id = r[0]
    if inspection_id:   kind, val = "inspection", inspection_id.upper()
    elif well_id:       kind, val = "well", well_id.upper()
    elif well_type:     kind, val = "well_type", well_type.upper()
    else:               kind, val = "all", "*"

    before = None
    orig = None
    if kind == "inspection":
        b = con.execute("SELECT * FROM v_insp WHERE inspection_id=?", (val,)).fetchone()
        if not b:
            return {"error": f"{val} not found"}
        before = dict(b)
        orig = con.execute(f"SELECT {field} FROM inspections WHERE inspection_id=?", (val,)).fetchone()[0]
    prev = _rows(con.execute("""SELECT c.id, u.name AS author, c.new_value, c.reason, c.created_at
        FROM mem.corrections c LEFT JOIN users u ON u.user_id=c.author_user_id
        WHERE c.active=1 AND c.scope_kind=? AND c.scope_value=? AND c.field=?""", (kind, val, field)))
    cur = con.execute("""INSERT INTO mem.corrections(created_at,author_user_id,scope_kind,scope_value,
        field,original_value,new_value,reason) VALUES (?,?,?,?,?,?,?,?)""",
        (now(), user_id, kind, val, field, None if orig is None else str(orig), str(new_value), reason))
    con.commit()
    cid = cur.lastrowid
    me = con.execute("SELECT name FROM users WHERE user_id=?", (user_id,)).fetchone()
    out = {"saved": True, "correction_id": cid, "scope": f"{kind}:{val}", "field": field,
           "original_value": orig, "new_value": float(new_value), "author": me[0] if me else user_id,
           "reason": reason, "original_kept": True, "citations": [f"CORR-{cid}"],
           "earlier_corrections_same_target": prev,
           "conflict_with_earlier_correction": [p for p in prev if str(p["new_value"]) != str(new_value)]}
    if kind == "inspection":
        a = dict(con.execute("SELECT * FROM v_insp WHERE inspection_id=?", (val,)).fetchone())
        insp = con.execute("SELECT u.name FROM users u WHERE u.user_id=?", (a["inspector_user_id"],)).fetchone()
        out["inspection"] = {"id": val, "well_id": a["well_id"], "date": a["inspected_date"],
                             "inspector": insp[0] if insp else None, "inspector_note": a["notes"]}
        out["citations"].append(val)
        out["producing_before"], out["producing_after"] = before["is_producing"], a["is_producing"]
        out["above_air_after_c"], out["threshold_c"] = a["delta_c"], a["threshold_c"]
        if a["is_producing"] == 1 and re.search(r"not moving|stopped|no stroke|shut|idle|not running|down",
                                                a["notes"] or "", re.I):
            out["conflict_with_inspector_note"] = (f"The same inspection's note says: \"{a['notes']}\" "
                                                   f"({out['inspection']['inspector']}). Ask the user to confirm.")
        if field == "spill_est_volume_bbl":
            vol = float(new_value)
            reportable = vol >= t.SR01_MIN_BBL
            ev = con.execute("""SELECT event_id, hours_to_report FROM regulator_events
                WHERE related_inspection_id=? AND event_type='spill_report_filed'""", (val,)).fetchone()
            rp = con.execute("""SELECT r.maintenance_id, rp.spill_class, rp.containment_hours
                FROM repairs r JOIN reports rp ON rp.maintenance_id=r.maintenance_id
                WHERE r.triggered_by_inspection_id=?""", (val,)).fetchone()
            imp = {"reportable": reportable, "original_ai_estimate_bbl": orig,
                   "spill_report_on_record": ev["event_id"] if ev else None}
            if reportable:
                imp["sr01"] = ("possible SR-01 breach: no spill report on record, due within "
                               f"{t.SR01_HOURS} h of the inspection at {a['inspected_at']}") if not ev else \
                              f"report {ev['event_id']} filed after {ev['hours_to_report']} h"
                imp["sr03_due_date"] = (date.fromisoformat(a["inspected_date"])
                                        + timedelta(days=t.SR03_DAYS)).isoformat()
            if rp:
                imp["repair_report"] = {"id": rp["maintenance_id"], "spill_class": rp["spill_class"],
                                        "containment_hours": rp["containment_hours"],
                                        "sr02_breach": bool(rp["containment_hours"] and rp["containment_hours"] > t.SR02_HOURS),
                                        "note": "the report's class and reporting statement were based on the old estimate"}
                out["citations"].append(rp["maintenance_id"])
            out["impact"] = imp
    return out


def save_rule(user_id, rule_key, value, well_type=None, well_id=None, note=""):
    import tools as t
    con = t.db()
    if well_id:     kind, val = "well", well_id.upper()
    elif well_type: kind, val = "well_type", well_type.upper()
    else:           kind, val = "all", "*"
    prev = _rows(con.execute("""SELECT r.id, u.name AS author, r.value, r.created_at FROM mem.rules r
        LEFT JOIN users u ON u.user_id=r.author_user_id
        WHERE r.active=1 AND r.rule_key=? AND r.scope_kind=? AND r.scope_value=?""", (rule_key, kind, val)))
    cur = con.execute("""INSERT INTO mem.rules(created_at,author_user_id,scope_kind,scope_value,rule_key,value,note)
        VALUES (?,?,?,?,?,?,?)""", (now(), user_id, kind, val, rule_key, str(value), note))
    con.commit()
    me = con.execute("SELECT name FROM users WHERE user_id=?", (user_id,)).fetchone()
    return {"saved": True, "rule_id": cur.lastrowid, "scope": f"{kind}:{val}", "rule_key": rule_key,
            "value": value, "author": me[0] if me else user_id,
            "conflict_with_earlier_rule": [p for p in prev if str(p["value"]) != str(value)],
            "citations": [f"RULE-{cur.lastrowid}"]}


def save_note(user_id, well_id, text, claim="other"):
    import tools as t
    con = t.db()
    well_id = well_id.upper()
    if not con.execute("SELECT 1 FROM wells WHERE well_id=?", (well_id,)).fetchone():
        return {"error": f"{well_id} not found"}
    li = con.execute("""SELECT i.inspection_id, i.inspected_at, i.delta_c, i.is_producing, i.threshold_c,
        u.name AS inspector FROM v_insp i LEFT JOIN users u ON u.user_id=i.inspector_user_id
        WHERE i.well_id=? ORDER BY i.inspected_at DESC LIMIT 1""", (well_id,)).fetchone()
    latest = dict(li) if li else {}
    prod = latest.get("is_producing")
    status, label = "recorded", "recorded (not checkable against inspections)"
    if prod is not None and claim in ("not_producing", "producing", "planned_shutdown"):
        if claim == "planned_shutdown":
            agrees = True   # a planned or ongoing shutdown is not contradicted by a stopped well; a future start is not checkable yet
        else:
            agrees = (prod == 0) if claim == "not_producing" else (prod == 1)
        if agrees:
            status = "consistent_with_data"
            label = ("consistent with the latest inspection: the well was not producing" if prod == 0
                     else "not contradicted: the planned shutdown starts after the latest inspection")
        else:
            status, label = "unconfirmed", "UNCONFIRMED: conflicts with the latest inspection"
    others = _rows(con.execute("""SELECT s.id, u.name AS author, s.text, s.claim, s.created_at
        FROM mem.shared_notes s LEFT JOIN users u ON u.user_id=s.author_user_id
        WHERE s.well_id=? AND s.author_user_id!=? ORDER BY s.id DESC LIMIT 5""", (well_id, user_id)))
    opp = lambda a, b: {a, b} in ({"producing", "not_producing"}, {"producing", "planned_shutdown"})
    disagree = [o for o in others if opp(o["claim"], claim)]
    if disagree and status != "unconfirmed":
        status, label = "unconfirmed", "UNCONFIRMED: another user says the opposite"
    cur = con.execute("INSERT INTO mem.shared_notes(created_at,author_user_id,well_id,text,claim,status) VALUES (?,?,?,?,?,?)",
                      (now(), user_id, well_id, text, claim, status))
    con.commit()
    official = _rows(con.execute("""SELECT n.note_id, u.name AS author, n.created_at, n.text FROM notes n
        LEFT JOIN users u ON u.user_id=n.author_user_id
        WHERE n.well_id=? AND n.note_type!='correction' ORDER BY n.created_at DESC LIMIT 3""", (well_id,)))
    me = con.execute("SELECT name FROM users WHERE user_id=?", (user_id,)).fetchone()
    cites = [f"NOTE-{cur.lastrowid}"] + ([latest["inspection_id"]] if latest else []) \
            + [o["note_id"] for o in official] + [f"NOTE-{o['id']}" for o in others]
    return {"saved": True, "note_id": f"NOTE-{cur.lastrowid}", "author": me[0] if me else user_id,
            "claim": claim, "status": status, "label": label, "latest_inspection": latest,
            "other_users_notes": others, "users_who_disagree": disagree, "official_notes": official,
            "instruction": "Both statements stay on record. If unconfirmed, say so and ask the user to confirm.",
            "citations": cites}


# ---------------------------------------------------------------- rules-driven analysis
def root_cause_candidates(scope, equipment_kind="pump_jack", months=12):
    import tools as t
    con = t.db()
    ids = t.resolve_scope(con, scope)
    if not ids:
        return {"error": f"no wells for scope {scope}"}
    start, end = t.days_ago(int(months * 30.4)), t.TODAY
    reps = t.rows(con.execute(f"""SELECT r.well_id, w.well_type, COUNT(*) AS n,
        GROUP_CONCAT(r.maintenance_id) AS repair_ids FROM repairs r JOIN wells w ON w.well_id=r.well_id
        WHERE r.well_id IN ({t.ph(ids)}) AND r.maintenance_type='corrective' AND r.equipment_kind=?
          AND r.started_date BETWEEN ? AND ? GROUP BY r.well_id HAVING n>=1
        ORDER BY n DESC, r.well_id""", ids + [equipment_kind, start, end]))
    flagged, close, cites = [], [], []
    for r in reps:
        rule = effective_rule(con, f"rca_{equipment_kind}", r["well_id"], r["well_type"])
        thr = int(float(rule["value"])) if rule else RCA_DEFAULT
        src = (f"rule by {rule['author']} on {rule['created_at'][:10]} (RULE-{rule['id']}, "
               f"{rule['scope_kind']} {rule['scope_value']}); overrides manual default {RCA_DEFAULT}"
               if rule else f"manual default ({RCA_DEFAULT})")
        ids_ = r["repair_ids"].split(",")
        item = {"well_id": r["well_id"], "well_type": r["well_type"], "corrective_repairs": r["n"],
                "repairs": ids_, "threshold": thr, "threshold_source": src,
                "added_only_by_user_rule": bool(rule and r["n"] < RCA_DEFAULT)}
        if r["n"] >= thr:
            flagged.append(item); cites += ids_ + ([f"RULE-{rule['id']}"] if rule else [])
        elif r["n"] >= 2:
            close.append(item)
    return {"scope": scope, "equipment_kind": equipment_kind, "window": [start, end],
            "flagged": flagged, "close_but_not_flagged": close, "citations": sorted(set(cites))}


# ---------------------------------------------------------------- private chat memory
def wells_in(*texts):
    seen = []
    for tx in texts:
        for w in re.findall(r"WELL-\d{4}", (tx or "").upper().replace("‑", "-")):
            if w not in seen:
                seen.append(w)
    return seen

def log_chat(user_id, chat_id, role, text, wells=None):
    con = sqlite3.connect(MEM_DB)
    con.executescript(schema(""))
    con.execute("INSERT INTO chat_log(ts,user_id,chat_id,role,text,wells) VALUES (?,?,?,?,?,?)",
                (now(), user_id, chat_id, role, text, ",".join(wells or [])))
    con.commit(); con.close()

def recall(user_id, limit=6):
    """Only ever reads rows of the CURRENT user. user_id comes from the app, not the model."""
    if not user_id:
        return {"error": "no user selected", "turns": [], "citations": []}
    con = sqlite3.connect(MEM_DB)
    con.executescript(schema(""))
    rs = con.execute("SELECT ts, chat_id, role, text, wells FROM chat_log WHERE user_id=? ORDER BY id DESC LIMIT 80",
                     (user_id,)).fetchall()[::-1]
    con.close()
    turns, q = [], None
    for ts, cid, role, text, wells in rs:
        if role == "user":
            q = (ts, text)
        elif q:
            turns.append({"asked_at": q[0], "question": q[1], "answer": text[:350],
                          "wells": [w for w in (wells or "").split(",") if w]})
            q = None
    turns = turns[::-1][:limit]
    if not turns:
        return {"turns": [], "count": 0, "no_earlier_conversation": True, "citations": [],
                "message": "NO earlier conversation on record for this user. Tell the user so. Do not guess wells."}
    return {"turns": turns, "count": len(turns), "citations": [],
            "note": "This user's own history only. If none of these turns contains the wells the user refers to, say there is no such earlier conversation for this user."}


def recent_activity(limit=5):
    import tools as t
    con = t.db()
    return {"corrections": _rows(con.execute("""SELECT c.id, u.name AS author, c.scope_value, c.field,
                c.original_value, c.new_value, c.reason FROM mem.corrections c
                LEFT JOIN users u ON u.user_id=c.author_user_id ORDER BY c.id DESC LIMIT ?""", (limit,))),
            "notes": _rows(con.execute("""SELECT s.id, u.name AS author, s.well_id, s.text, s.status
                FROM mem.shared_notes s LEFT JOIN users u ON u.user_id=s.author_user_id
                ORDER BY s.id DESC LIMIT ?""", (limit,))),
            "rules": _rows(con.execute("""SELECT r.id, u.name AS author, r.scope_kind, r.scope_value, r.rule_key, r.value
                FROM mem.rules r LEFT JOIN users u ON u.user_id=r.author_user_id ORDER BY r.id DESC LIMIT ?""", (limit,)))}


# ---------------------------------------------------------------- context readers
def well_context(well_id):
    import tools as t
    con = t.db()
    well_id = well_id.upper()
    official = _rows(con.execute("""SELECT n.note_id, u.name AS author, n.created_at, n.note_type, n.inspection_id, n.text
        FROM notes n LEFT JOIN users u ON u.user_id=n.author_user_id
        WHERE n.well_id=? ORDER BY n.created_at DESC LIMIT 8""", (well_id,)))
    shared = shared_notes_for(con, well_id)
    return {"well_id": well_id, "shared_notes": shared, "official_notes": official,
            "corrections": corrections_for(con, well_id),
            "citations": [f"NOTE-{s['id']}" for s in shared] + [o["note_id"] for o in official]}


def check_spill(well_id, date_str):
    import tools as t
    con = t.db()
    r = con.execute("""SELECT i.inspection_id, i.inspected_date, i.spill_detected, i.spill_corrected, i.real_spill,
        i.spill_est_volume_bbl, i.notes, u.name AS inspector FROM v_insp i
        LEFT JOIN users u ON u.user_id=i.inspector_user_id
        WHERE i.well_id=? AND i.inspected_date=?""", (well_id.upper(), str(date_str)[:10])).fetchone()
    if not r:
        return {"error": f"no inspection of {well_id} on {date_str}"}
    r = dict(r)
    notes = _rows(con.execute("""SELECT n.note_id, u.name AS author, n.note_type, n.text FROM notes n
        LEFT JOIN users u ON u.user_id=n.author_user_id
        WHERE n.well_id=? AND (n.inspection_id=? OR n.text LIKE ?)""",
        (well_id.upper(), r["inspection_id"], f"%{r['inspection_id']}%")))
    allw = _rows(con.execute("""SELECT n.note_id, u.name AS author, n.note_type, n.text FROM notes n
        LEFT JOIN users u ON u.user_id=n.author_user_id WHERE n.well_id=?""", (well_id.upper(),)))
    seen = {n["note_id"] for n in notes}
    notes += [n for n in allw if n["note_id"] not in seen
              and re.search(r"spill|stain|sheen|crude|water|oil", n["text"], re.I)]
    rep = _rows(con.execute("""SELECT r.maintenance_id, r.issue, rp.containment_hours, rp.spill_class
        FROM repairs r LEFT JOIN reports rp ON rp.maintenance_id=r.maintenance_id
        WHERE r.triggered_by_inspection_id=?""", (r["inspection_id"],)))
    return {"inspection": r, "formal_correction": bool(r["spill_corrected"]),
            "notes_about_it": notes, "spill_repairs": rep,
            "rule": "Notes that disagree are NOT formal corrections. The detection stands unless a formal correction exists. Show every note with its author.",
            "citations": [r["inspection_id"]] + [n["note_id"] for n in notes] + [x["maintenance_id"] for x in rep]}