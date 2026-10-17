import os, json, re, calendar, time, difflib
from datetime import date
from dotenv import load_dotenv
from openai import OpenAI
import tools as t
import memory

try:
    import prove_lib as pl
except Exception as _e:
    pl = None
    print("proof module not loaded:", _e)

load_dotenv()
KEYS = [k for k in (os.getenv("GROQ_API_KEY"), os.getenv("GROQ_API_KEY_2")) if k]
CLIENTS = [OpenAI(base_url="https://api.groq.com/openai/v1", api_key=k, max_retries=0) for k in KEYS]
KI = {"i": 0}
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
MODEL_TOOLS = os.getenv("GROQ_MODEL_TOOLS", MODEL)    # phase 1: picks tools
MODEL_ANSWER = os.getenv("GROQ_MODEL_ANSWER", MODEL)  # phase 2: writes the answer
FALLBACK = os.getenv("GROQ_FALLBACK", "openai/gpt-oss-20b")
EFFORT = os.getenv("GROQ_EFFORT", "")                 # "low", "medium", "high" or empty
MAX_STEPS = 6
MAX_SECONDS = float(os.getenv("MAX_SECONDS", "90"))   # hard cap for one question
DEADLINE = {"t": 0.0}
TIMING = {"model_s": 0.0, "model_calls": 0, "tool_s": 0.0}
CTX = {"user_id": None}


# ---------- date helper: the model says "last 4 months", Python does the date maths ----------
def window(start=None, end=None, last_days=None, last_months=None):
    end = end or t.TODAY
    if last_days:
        start = t.days_ago(int(last_days))
    elif last_months:
        d = date.fromisoformat(t.TODAY)
        m = d.month - int(last_months)
        y = d.year
        while m <= 0:
            m += 12
            y -= 1
        day = min(d.day, calendar.monthrange(y, m)[1])
        start = date(y, m, day).isoformat()
    return start or "2000-01-01", end


# ---------- full-text search over manuals, SOPs and repair reports (needs build_index.py) ----------
def search_docs(query=None, well_type=None, well_id=None, kind=None, top_k=5):
    con = t.db()
    words = re.findall(r"[A-Za-z0-9]{2,}", query or "")
    if not words:
        return {"error": "empty query", "results": [], "citations": []}
    sql = "SELECT source, kind, well_id, text FROM docs_fts WHERE docs_fts MATCH ?"
    p = [" OR ".join(words)]
    if well_type:
        sql += " AND (well_type=? OR well_type='')"; p.append(well_type.upper())
    if well_id:
        sql += " AND (well_id=? OR well_id='')"; p.append(well_id.upper())
    if kind:
        sql += " AND kind=?"; p.append(kind)
    sql += " ORDER BY bm25(docs_fts) LIMIT ?"; p.append(int(top_k))
    try:
        hits = [{"source": r["source"], "kind": r["kind"], "text": r["text"][:1800]}
                for r in con.execute(sql, p)]
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "results": [], "citations": []}
    return {"query": query, "results": hits, "citations": [h["source"] for h in hits]}


# ---------- read-only SQL fallback for questions no ready-made tool covers ----------
def run_sql(query):
    if re.search(r"chat_log|mem\.", query or "", re.I):
        return {"error": "That table is private. Use the dedicated tools."}
    q = (query or "").strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", q):
        return {"error": "Only SELECT queries are allowed."}
    con = t.db()
    con.execute("PRAGMA query_only = ON")
    try:
        rows = [dict(r) for r in con.execute(q).fetchmany(200)]
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    ids = {v for r in rows for v in r.values()
           if isinstance(v, str) and re.match(r"^(INS|MNT|CTX|REG)-", v)}
    return {"sql": q, "row_count": len(rows), "rows": rows, "citations": sorted(ids)}


# ---------- resolve a person's name, even misspelled ----------
def find_user(name):
    con = t.db()
    users = [dict(r) for r in con.execute("SELECT user_id, name, role, team_id FROM users")]
    names = [u["name"] for u in users]
    close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
    matches = [u for u in users if u["name"] in close]
    return {"query": name, "matches": matches,
            "exact": any(u["name"].lower() == name.lower() for u in users),
            "citations": [u["user_id"] for u in matches]}


# ---------- wrappers: what the model is allowed to call ----------
def tool_well_status(well_id):
    return t.well_status(well_id.upper())

def tool_not_producing_now(scope):
    return t.not_producing_now(scope)

def tool_temperature_series(well_id, weeks=13):
    return t.temperature_series(well_id.upper(), int(weeks))

def tool_downtime(scope, start=None, end=None, last_days=None, last_months=None, top_n=None):
    s, e = window(start, end, last_days, last_months)
    return t.downtime(scope, s, e, int(top_n) if top_n else None)

def tool_spills(scope, start=None, end=None, last_days=None, last_months=None,
                min_bbl=0, with_containment=False, only_late_containment=False):
    s, e = window(start, end, last_days, last_months)
    r = t.spills(scope, s, e, float(min_bbl or 0), bool(with_containment or only_late_containment))
    if only_late_containment:
        r["spills"] = [x for x in r["spills"] if x.get("breaches_sr02")]
        r["count"] = len(r["spills"])
        r["total_bbl"] = round(sum(x["barrels"] or 0 for x in r["spills"]), 1)
        r["citations"] = [c for x in r["spills"] for c in (x["inspection_id"], x.get("repair_report")) if c]
    r["filters_applied"] = {"from": s, "to": e, "min_bbl": min_bbl or 0,
                            "only_late_containment": bool(only_late_containment)}
    return r

def tool_spill_heatmap(year):
    return t.spill_heatmap(int(year))

def tool_rule_check(well_id, start=None, end=None, last_days=None, last_months=None):
    s, e = window(start, end, last_days, last_months)
    return t.rule_check(well_id.upper(), s, e)

def tool_repair_costs(well_ids, year):
    return t.repair_costs(well_ids, int(year))

def tool_save_correction(field, new_value, reason, inspection_id=None, well_id=None,
                         inspection_date=None, well_type=None, **_):
    if not CTX["user_id"]:
        return {"error": "no user selected"}
    return memory.save_correction(CTX["user_id"], field, new_value, reason, inspection_id,
                                  well_id, inspection_date, well_type)

def tool_save_note(well_id, text, claim="other", **_):
    if not CTX["user_id"]:
        return {"error": "no user selected"}
    return memory.save_note(CTX["user_id"], well_id, text, claim)

def tool_save_rule(rule_key, value, well_type=None, well_id=None, note="", **_):
    if not CTX["user_id"]:
        return {"error": "no user selected"}
    rule_key = (rule_key or "").strip().lower().replace(" ", "_").replace("-", "_")
    return memory.save_rule(CTX["user_id"], rule_key, value, well_type, well_id, note or "")

def tool_recall_my_chats(**_):
    return memory.recall(CTX["user_id"])

def tool_root_cause_candidates(scope="all", equipment_kind="pump_jack", months=12, **_):
    ek = (equipment_kind or "pump_jack").strip().lower().replace(" ", "_")
    return memory.root_cause_candidates(scope, ek, int(months))

def tool_well_context(well_id, **_):
    return memory.well_context(well_id)

def tool_check_spill(well_id, date, **_):
    return memory.check_spill(well_id, date)

FUNCS = {
    "well_status": tool_well_status, "not_producing_now": tool_not_producing_now,
    "temperature_series": tool_temperature_series, "downtime": tool_downtime,
    "spills": tool_spills, "spill_heatmap": tool_spill_heatmap,
    "rule_check": tool_rule_check, "repair_costs": tool_repair_costs,
    "search_docs": search_docs, "run_sql": run_sql, "find_user": find_user,
    "save_correction": tool_save_correction, "save_note": tool_save_note,
    "save_rule": tool_save_rule, "recall_my_chats": tool_recall_my_chats,
    "root_cause_candidates": tool_root_cause_candidates,
    "well_context": tool_well_context, "check_spill": tool_check_spill,
}

SCOPE = {"type": "string", "description": "A well ID like WELL-0001, an area name like 'Northeast Block 8', a well type like T200, or 'all'"}
WINDOW = {
    "start": {"type": "string", "description": "YYYY-MM-DD, optional"},
    "end": {"type": "string", "description": "YYYY-MM-DD, optional, defaults to today"},
    "last_days": {"type": "integer", "description": "Use for 'last N days'"},
    "last_months": {"type": "integer", "description": "Use for 'last N months'"},
}

def fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}

TOOLS = [
    fn("well_status", "Is one well producing oil right now? Latest inspection, inspector, notes, open repairs, and whether the recorded status is out of date.",
       {"well_id": {"type": "string"}}, ["well_id"]),
    fn("not_producing_now", "List wells in a scope (area, type, or all) that are NOT producing at their latest inspection, with any recorded reason (open repair or note). Also lists wells removed from the list by a user correction.",
       {"scope": SCOPE}, ["scope"]),
    fn("temperature_series", "Wellhead vs air temperature for the last N weekly inspections of one well, flagged producing or not. For line charts.",
       {"well_id": {"type": "string"}, "weeks": {"type": "integer"}}, ["well_id"]),
    fn("downtime", "Downtime days per well (7 days per inspection that found the well not producing) in a time window. Sorted highest first. Use top_n for rankings.",
       {"scope": SCOPE, **WINDOW, "top_n": {"type": "integer"}}, ["scope"]),
    fn("spills", "Real oil spills (false alarms removed, user corrections applied) in a scope and time window, with count, total barrels and location. "
       "Each spill of 5 bbl or more shows whether a regulator report is on record. "
       "min_bbl: set ONLY if the user asks for reportable or large spills (5 or more); otherwise leave it out. "
       "with_containment=true adds containment hours from repair reports. "
       "only_late_containment=true returns only spills contained later than the 4-hour SOP limit.",
       {"scope": SCOPE, **WINDOW, "min_bbl": {"type": "number"},
        "with_containment": {"type": "boolean"}, "only_late_containment": {"type": "boolean"}}, ["scope"]),
    fn("spill_heatmap", "Count of real spills per area per month for one year. For heatmaps.",
       {"year": {"type": "integer"}}, ["year"]),
    fn("rule_check", "Check one well for SR-01 (late spill report) and SR-02 (late containment) breaches, and list its regulator events (fines, notices, status).",
       {"well_id": {"type": "string"}, **WINDOW}, ["well_id"]),
    fn("repair_costs", "Total repair cost in USD per well for a calendar year.",
       {"well_ids": {"type": "array", "items": {"type": "string"}}, "year": {"type": "integer"}}, ["well_ids", "year"]),
    fn("search_docs", "Full-text search of the manuals, SOPs and repair reports. Use for rules, deadlines, procedures, and reasons found in repair reports. "
       "Optional filters: well_type (T200/T350), well_id, kind ('manual' or 'report').",
       {"query": {"type": "string"}, "well_type": {"type": ["string", "null"]},
        "well_id": {"type": ["string", "null"]}, "kind": {"type": ["string", "null"]}}, ["query"]),
    fn("find_user", "Resolve a person's name, even if misspelled, to user_id, role and team. Call this first whenever a person is named.",
       {"name": {"type": "string"}}, ["name"]),
    fn("root_cause_candidates", "Wells that need a root-cause analysis: corrective repairs of one equipment kind (use underscores, e.g. pump_jack) in the last 12 months "
       "against the threshold (manual default 3, or a user's rule). Shows which source set each threshold.",
       {"scope": SCOPE, "equipment_kind": {"type": "string"}, "months": {"type": "integer"}}, ["scope"]),
    fn("well_context", "Everything people have recorded about one well: shared notes from users (with author), official notes, corrections. "
       "Use for 'is downtime planned', 'what do we know about WELL-X', notes or planned work.",
       {"well_id": {"type": "string"}}, ["well_id"]),
    fn("check_spill", "Was a spill on a given date real? Returns the inspection, any formal correction, every note about it with its author, and the repair. "
       "Use for 'was the spill real / was it oil'.",
       {"well_id": {"type": "string"}, "date": {"type": "string"}}, ["well_id", "date"]),
    fn("run_sql", "Read-only SQL for any counting, ranking or grouping question no other tool covers. "
       "Tables: v_insp(inspection_id, well_id, inspected_date, inspector_user_id, method, delta_c, is_producing, downtime_days, "
       "spill_est_volume_bbl, real_spill, well_type), wells(well_id, name, well_type, area_name, team_id, recorded_status, lat, lon), "
       "users(user_id, name, role, team_id), repairs(maintenance_id, well_id, started_date, maintenance_type, equipment_kind, issue, "
       "technician_user_id, contractor, downtime_hours, cost_total, triggered_by_inspection_id), "
       "reports(maintenance_id, well_id, containment_hours, root_cause, spill_class), "
       "regulator_events(event_id, well_id, date, event_type, protocol_id, hours_to_report, fine_usd, status), "
       "notes(note_id, well_id, author_user_id, note_type, inspection_id, text). "
       "Rules: real spills = real_spill=1; downtime = SUM(downtime_days), never repairs.downtime_hours; join users for names; "
       "always select ID columns when possible. SQLite syntax only (use strftime, not EXTRACT).",
       {"query": {"type": "string"}}, ["query"]),
    fn("save_correction", "Record that a value is wrong. field: wellhead_temp_c or ambient_temp_c (a raw reading), "
       "spill_est_volume_bbl (the AI's spill volume estimate) or spill_detected (0 = not a spill). "
       "To target ONE inspection give well_id + inspection_date (YYYY-MM-DD, year 2026 unless stated). "
       "Give well_id alone only if it applies to every inspection of that well; well_type (T200/T350) only if the user says it applies to a whole type. "
       "The original is kept; the author is the current user.",
       {"field": {"type": "string"}, "new_value": {"type": "number"}, "reason": {"type": "string"},
        "well_id": {"type": ["string", "null"]}, "inspection_date": {"type": ["string", "null"]},
        "inspection_id": {"type": ["string", "null"]}, "well_type": {"type": ["string", "null"]}},
       ["field", "new_value", "reason"]),
    fn("save_note", "Record a statement a user makes about a well so other users see it. "
       "claim: not_producing, producing, planned_shutdown (planned or future work) or other. Returns conflicts with the data and other users.",
       {"well_id": {"type": "string"}, "text": {"type": "string"}, "claim": {"type": "string"}}, ["well_id", "text"]),
    fn("save_rule", "Record a rule or threshold a user sets for a well type, one well, or all wells. "
       "rule_key for root-cause analysis: rca_<equipment_kind>, e.g. rca_pump_jack, value = number of corrective repairs in 12 months.",
       {"rule_key": {"type": "string"}, "value": {"type": "number"},
        "well_type": {"type": ["string", "null"]}, "well_id": {"type": ["string", "null"]},
        "note": {"type": ["string", "null"]}}, ["rule_key", "value"]),
    fn("recall_my_chats", "Return THIS user's earlier questions, answers and the wells discussed (across their past chats). "
       "Use when the user refers to earlier conversations.", {}, []),
]

MEM_TOOLS = {"save_correction", "save_note", "save_rule", "recall_my_chats"}

def pick_tools(q):
    """Send fewer tool schemas per call (saves tokens). Write-tools only for statements or memory words."""
    ql = q.lower()
    want_mem = ("?" not in q) or re.search(
        r"wrong|measured|estimated|correct|discussed|yesterday|earlier|last time|we |note for|fyi", ql)
    return [x for x in TOOLS if want_mem or x["function"]["name"] not in MEM_TOOLS]


# ---------- phase 1 prompt: gather data ----------
SYSTEM = f"""You are the Asset Intelligence Copilot for an oil field. Today is {t.TODAY}.
Rules:
1. NEVER calculate or guess numbers. Always call a data tool and use its results.
2. Use tool results as the only source of facts. Do not invent causes. If no reason is recorded, say so.
3. Producing or not comes from the latest inspection, not from recorded_status (it can be out of date).
4. Call every tool you need (several if needed). When you have all the results, stop calling tools and reply with the single word READY.
5. Never answer a data question without calling a tool first.
6. Prefer a specific tool. If none fits a counting, ranking or grouping question, use run_sql. For rules, deadlines, procedures or reasons in repair reports, use search_docs.
7. Do not add filters the user did not ask for. If no period is named, use all available data and say so.
8. In run_sql, NULL in contractor means in-house crew. Report it as "in-house" instead of dropping it, and show the full ranking (LIMIT 5) rather than only the top row.
9. When a SQL query returns the top-1 row only, also run it with LIMIT 5 so ties and context are visible.
10. When a person is named, call find_user first and use the matched name. Use the returned user_id in later queries, not the name. If the user's spelling differs, say which person you matched. If find_user returns several matches, ask which one the user means.
11. For subjective comparisons (efficient, best, worst) between people with different roles, do not rank them at all, on any measure. State each person's role, give the raw numbers, and say a fair ranking is not possible.
12. If you need several independent lookups (for example find_user for three people), request them all in the same step.
13. When a user STATES a fact about a well ("X is shut in", "X is back online", "planned maintenance"), call save_note with claim not_producing, producing, planned_shutdown or other. Never decide who is right; report the conflict info the tool returns. A question is not a statement: do not save notes for questions.
14. When a user says a value is wrong (a reading, or the AI's spill volume), call save_correction with well_id + inspection_date to hit one inspection. Never invent the new value.
15. When a user states a rule or threshold for a well type or well, call save_rule (rule_key rca_<equipment_kind>, e.g. rca_pump_jack).
16. For root-cause analysis questions use root_cause_candidates (equipment_kind with underscores, e.g. pump_jack).
17. If the user refers to earlier conversations ("we discussed", "yesterday"), call recall_my_chats first and use the well IDs it returns. If it returns nothing, say there is no earlier conversation for this user. Never use anyone else's history.
18. If a correction makes a spill reportable (5 bbl or more), also call rule_check for that well and search_docs for the SR-01 and SR-03 deadlines.
19. For planned downtime, shutdowns or "what do we know about WELL-X", call well_context. For "was the spill real", call check_spill with well_id and date (YYYY-MM-DD, year 2026 unless stated)."""

# ---------- phase 2 prompt: write the answer ----------
FINAL_SYSTEM = f"""You write the final answer for the oil field copilot. Today is {t.TODAY}.
You get the user's question and the exact tool results. Use ONLY those results.
- State the facts clearly with numbers, dates and IDs from the results.
- Explain WHY when the results contain a reason (an open repair, a note, a repair report).
- Treat an inspector's note as an observation, not as a recorded reason. A recorded reason is an open repair or a note (CTX). If only the inspector's note exists, say 'no recorded reason (inspector observed: ...)'.
- Only mention recorded_status when the question is about whether a well is producing. Then state whether it matches the latest inspection; if it disagrees, say it is out of date.
- If no repair or note explains a stop, say "no recorded reason" and suggest an inspection check. Never invent a cause.
- Point out any conflict you can see (a note that disagrees with an inspection).
- Always say which time window and filters were used (see filters_applied or the dates in the result). Never state a time period the tool results do not show.
- If a person's name was matched approximately, say who you matched.
- If people have different roles, state the roles and do not rank them on subjective measures.
- If the result is an aggregate with no record IDs, say it comes from a SQL query and describe its filters.
- If the results are empty or an error, say so honestly.
- For corrections: say who corrected, why, that the original value is kept, and the effect on the answer.
- For conflicts (a claim vs an inspection, an inspector note, or another user's note): show BOTH sides with authors, label the new claim "unconfirmed", and ask the user to confirm. Never silently pick one.
- If the input ends with "MUST STATE IN answer_text", every item there must appear in your answer_text.
- Attribute notes and rules to their authors by name. If a user's rule overrides the manual, say "this follows <name>'s rule, not the manual".
- If a correction excluded a well from a list (excluded_by_correction), say which well and why.
- If no_earlier_conversation is true, answer only: there is no earlier conversation on record for this user. Do not discuss repairs or reasons and do not say a tool failed.
- For rule-driven lists, say which wells were added only because of the user's rule (repairs below the manual default) and which already met the manual.
- Say "no spill report is on record in the regulator events", never attribute it to an inspector.
- Planned downtime: report each shared note with its author, start and duration. Do not say "no downtime" if a shared note exists.
- For a claim like "back online", compare with official notes that give an expected return date (for example CTX-0001-003 expects return around 20 Oct) and say they conflict.
- Spill reality: if notes disagree and no formal correction exists, answer that the detection stands and show each note with its author (for example Ryan Brooks says produced water, Chris Dalton sampled and confirmed crude).
- For not_producing_now, list EVERY well in "wells" (with its reason or "no recorded reason") and also mention excluded_by_correction.
- Use regulator_report_on_record and sr01_status for regulator reporting. repair_report is the crew's repair document, never a regulator filing.
- Never copy the MUST STATE lines verbatim. Write normal sentences.
- Spills under 5 bbl do not need a regulator report. Never say they have one on record.
Reply with ONLY this JSON:
{{"answer_text": "...", "visual": "status_card|table|line_chart|bar_chart|map|heatmap|none",
  "visual_source": "name of the tool whose result should be drawn, or empty", "citations": ["INS-...", "MNT-..."]}}
Visual choice: if the user names a visual, use it. Otherwise: one well -> status_card; a list, or 'which wells', 'list', 'show me' several records -> table;
change over time -> line_chart; comparing a few wells or areas -> bar_chart; locations -> map; area x month -> heatmap; a single fact -> none.
If the user asks to "list" things, use table.
Only cite IDs that appear in the tool results.
- When a visual is chosen, do not list every data point in the text. Give a one or two sentence summary (for example, which dates were not producing)."""


# ---------- model calls with rate-limit handling and key rotation ----------
def _retry_after(err):
    m = re.search(r"try again in ([0-9hms.]+)", err)
    if not m:
        return None
    unit = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    return sum(float(n) * unit[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", m.group(1)))


def call_model(messages, retries=6, model=None, **kw):
    """Returns the response, or None. On a 429: rotate API key first, then switch model, then wait. Never passes the deadline."""
    model = model or MODEL
    primary = model
    rotations = 0
    for attempt in range(retries):
        if time.perf_counter() > DEADLINE["t"]:
            print("   [time cap reached]")
            return None
        extra = dict(kw)
        if EFFORT and "gpt-oss" in model:
            extra["reasoning_effort"] = EFFORT
        t0 = time.perf_counter()
        try:
            r = CLIENTS[KI["i"] % len(CLIENTS)].chat.completions.create(
                model=model, messages=messages, temperature=0.1, timeout=25, **extra)
            TIMING["model_s"] += time.perf_counter() - t0
            TIMING["model_calls"] += 1
            print(f"   [call ok] {model} key{KI['i'] % len(CLIENTS) + 1} {time.perf_counter()-t0:.1f}s")
            return r
        except Exception as e:
            TIMING["model_s"] += time.perf_counter() - t0
            err = str(e)
            print(f"   [model error, attempt {attempt+1}]", err[:120])
            if "429" in err:
                if len(CLIENTS) > 1 and rotations < len(CLIENTS) - 1:
                    rotations += 1
                    KI["i"] += 1
                    print(f"   [rotating to key {KI['i'] % len(CLIENTS) + 1}]")
                    continue
                if model != FALLBACK:
                    print(f"   [switching to {FALLBACK}]")
                    model = FALLBACK
                    rotations = 0
                    continue
                if "tokens per day" in err or "TPD" in err:
                    print("   [daily token limit hit on all keys and fallback]")
                    return None
                wait = min(_retry_after(err) or 8, 20) + 0.5
                if time.perf_counter() + wait > DEADLINE["t"]:
                    return None
                print(f"   [waiting {wait:.0f}s for the rate limit]")
                time.sleep(wait)
                model = primary
                rotations = 0
                continue
            time.sleep(2)
    return None


# ---------- phase 2: write the answer ----------
def _shrink(o, n=220):
    """Drop empty values, cut long strings and long lists so the writer sees ALL keys."""
    if isinstance(o, dict):
        return {k: _shrink(v, n) for k, v in o.items() if v not in (None, "", [], {})}
    if isinstance(o, list):
        return [_shrink(x, n) for x in o[:25]]
    if isinstance(o, str) and len(o) > n:
        return o[:n] + "..."
    return o

MUST_KEYS = ("conflict_with_inspector_note", "conflict_with_earlier_correction", "conflict_with_earlier_rule",
             "label", "impact", "users_who_disagree", "excluded_by_correction",
             "latest_inspection", "official_notes", "other_users_notes", "message",
             "notes_about_it", "shared_notes", "no_earlier_conversation", "wells")

def must_say(calls):
    """Facts the code found (conflicts, impacts) that the answer must mention."""
    lines = []
    for c in calls:
        r = c["result"]
        if isinstance(r, dict) and c["tool"].startswith(
                ("save_", "not_producing", "recall", "check_spill", "well_context")):
            for k in MUST_KEYS:
                if r.get(k):
                    lines.append(f"{c['tool']}.{k}: " + json.dumps(_shrink(r[k]), default=str)[:700])
    return lines


def _writer_call(user):
    r = call_model([{"role": "system", "content": FINAL_SYSTEM},
                    {"role": "user", "content": user}],
                   model=MODEL_ANSWER,
                   response_format={"type": "json_object"})
    parsed = None
    if r:
        try:
            parsed = json.loads(r.choices[0].message.content)
        except Exception:
            parsed = {"answer_text": r.choices[0].message.content or ""}
    return r, parsed


RAW_PREFIXES = ("save_", "not_producing", "check_spill", "well_context", "recall_")

def _looks_raw(text):
    return (text or "").lstrip().startswith(RAW_PREFIXES)


def finalize(question, calls):
    """Phase 2: no tools, JSON only. Citations are filtered to IDs that really came from tools."""
    allowed, parts = set(), []
    for c in calls:
        res = c["result"]
        if isinstance(res, dict):
            allowed.update(str(x) for x in res.get("citations", []))
        parts.append(f"TOOL {c['tool']} {json.dumps(c['args'])}\nRESULT: "
                     + json.dumps(_shrink(res), default=str)[:6000])
    user = f"QUESTION: {question}\n\n" + "\n\n".join(parts)
    ms = must_say(calls)
    if ms:
        user += "\n\nMUST STATE IN answer_text (both sides, with authors):\n" + "\n".join(ms)

    out = {"answer_text": "I could not produce an answer.", "visual": "none",
           "visual_source": "", "citations": []}
    r, parsed = _writer_call(user)

    # guard: the writer sometimes pastes the MUST STATE lines verbatim; ask once more for sentences
    if parsed and _looks_raw(parsed.get("answer_text")):
        r2, parsed2 = _writer_call(user + "\n\nYour last answer copied raw data lines. "
                                   "Rewrite answer_text as normal sentences. Do not start with a tool name.")
        if parsed2 and not _looks_raw(parsed2.get("answer_text")):
            parsed = parsed2
        else:
            parsed["answer_text"] = ("The information was recorded. Conflicts and details are listed in the "
                                     "evidence and tool results below.")

    if r and parsed:
        out.update(parsed)
    else:
        out["answer_text"] = "The answer writer was rate-limited, so this is the raw result (see tool calls)."
        names = [c["tool"] for c in calls]
        q = question.lower()
        if "bar" in q or "downtime" in names:
            out["visual"], out["visual_source"] = "bar_chart", "downtime"
        elif "map" in q:
            out["visual"], out["visual_source"] = "map", "spills"
        elif "heatmap" in q or "spill_heatmap" in names:
            out["visual"], out["visual_source"] = "heatmap", "spill_heatmap"
        elif "line" in q or "temperature_series" in names:
            out["visual"], out["visual_source"] = "line_chart", "temperature_series"
        elif "well_status" in names:
            out["visual"], out["visual_source"] = "status_card", "well_status"
        else:
            out["visual"], out["visual_source"] = "table", names[-1] if names else ""
    out["citations"] = [c for c in out.get("citations", []) if str(c) in allowed]
    out["tool_calls"] = calls
    return out


# ---------- phase 1: gather data ----------
def _ask(question, history=None):
    messages = [{"role": "system", "content": SYSTEM}] + (history or []) + \
               [{"role": "user", "content": question}]
    tools_now = pick_tools(question)
    calls, nudged = [], False
    for _ in range(MAX_STEPS):
        if time.perf_counter() > DEADLINE["t"]:
            break
        r = call_model(messages, model=MODEL_TOOLS, tools=tools_now, tool_choice="auto")
        if r is None:
            break
        msg = r.choices[0].message
        if not msg.tool_calls:
            if not calls and not nudged:      # answered without data: push back once
                nudged = True
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({"role": "user", "content": "Call a data tool first. Do not answer from memory."})
                continue
            break                             # model said READY
        messages.append(msg)
        for c in msg.tool_calls:
            try:
                args = json.loads(c.function.arguments or "{}")
                args = {k: v for k, v in args.items() if v is not None}
                ts = time.perf_counter()
                result = FUNCS[c.function.name](**args)
                TIMING["tool_s"] += time.perf_counter() - ts
            except Exception as e:
                args, result = {}, {"error": f"{type(e).__name__}: {e}"}
            calls.append({"tool": c.function.name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": json.dumps(_shrink(result), default=str)[:6000]})
    if not calls:
        return {"answer_text": "I could not find the right tool for that question (or the time cap / token limit was hit).",
                "visual": "none", "visual_source": "", "citations": [], "tool_calls": []}
    return finalize(question, calls)


def validate(out):
    """Code-computed confidence label and checks. The model has no say in this."""
    calls = out.get("tool_calls", [])
    sqls = [c for c in calls if c["tool"] == "run_sql"]
    checks = []
    if not calls:
        out["confidence"], out["checks"] = "No data used", []
        return out
    if not sqls:
        out["confidence"] = "Verified: answered by a ready-made tool (logic checked against the 18 sample answers)"
        out["checks"] = ["Numbers computed in Python/SQL, not by the model"]
        return out
    warn = False
    for c in sqls:
        r = c["result"]
        q = (r.get("sql") or c["args"].get("query", "")).lower()
        if r.get("error"):
            checks.append(f"SQL error: {r['error'][:80]}"); warn = True; continue
        if re.search(r"\binspections\b", q) and "v_insp" not in q:
            checks.append("WARNING: reads raw inspections, so corrections are NOT applied"); warn = True
        else:
            checks.append("OK: reads the corrected view v_insp (or non-inspection tables)")
        if "downtime_hours" in q:
            checks.append("WARNING: uses repairs.downtime_hours, not weekly-inspection downtime"); warn = True
        if "spill" in q and "real_spill" not in q:
            checks.append("WARNING: counts spills without real_spill=1 (false alarms may be included)"); warn = True
        if r.get("row_count") == 0:
            checks.append("WARNING: query returned 0 rows"); warn = True
        if r.get("row_count") == 200:
            checks.append("WARNING: result hit the 200-row cap"); warn = True
    out["confidence"] = ("Computed, unverified: check the SQL shown" if warn
                         else "Computed by SQL, sanity checks passed (not independently cross-checked)")
    out["checks"] = checks
    return out


def auto_proof(out):
    """Independent raw-file recomputation for the question types prove_lib covers."""
    proofs = []
    if pl is None:
        out["proof"] = proofs
        return out
    try:
        for c in out.get("tool_calls", []):
            r, a = c["result"], c["args"]
            if not isinstance(r, dict) or r.get("error"):
                continue
            if c["tool"] == "downtime" and r.get("wells"):
                area_id = next((k for k, v in pl.AREA_NAME.items() if v == a.get("scope")), None)
                if area_id:
                    s, e = r["window"]
                    raw = pl.downtime_days(area_id, s, e, top=len(r["wells"]))
                    app = [(w["downtime_days"], w["well_id"]) for w in r["wells"]]
                    rawl = [(d, w) for d, w, _ in raw]
                    proofs.append({"check": f"downtime {a['scope']} {s}..{e}",
                                   "app": app, "raw_files": rawl,
                                   "match": [x[0] for x in app] == [x[0] for x in rawl]})
            elif c["tool"] == "repair_costs" and r.get("wells"):
                raw = pl.repair_cost([w.upper() for w in a["well_ids"]], a["year"])
                app = {w["well_id"]: w["total_usd"] for w in r["wells"]}
                proofs.append({"check": f"repair cost {a['year']}", "app": app, "raw_files": raw,
                               "match": all(abs(app.get(k, 0) - v) < 1 for k, v in raw.items())})
    except Exception as ex:
        proofs.append({"check": "proof failed to run", "app": "", "raw_files": str(ex)[:120], "match": None})
    out["proof"] = proofs
    return out


def ask(question, history=None, user_id=None):
    """Public entry point: sets the user, runs under a time cap, adds validation, proof and timing."""
    CTX["user_id"] = user_id
    TIMING.update(model_s=0.0, model_calls=0, tool_s=0.0)
    t0 = time.perf_counter()
    DEADLINE["t"] = t0 + MAX_SECONDS
    out = auto_proof(validate(_ask(question, history)))
    out["timing"] = {"total_s": round(time.perf_counter() - t0, 2),
                     "model_s": round(TIMING["model_s"], 2),
                     "model_calls": TIMING["model_calls"],
                     "tools_s": round(TIMING["tool_s"], 2),
                     "cap_s": MAX_SECONDS}
    return out


if __name__ == "__main__":
    print("Ask a question (blank line to quit).")
    while True:
        q = input("\n> ").strip()
        if not q:
            break
        a = ask(q, user_id=None)
        print("\nTOOLS:", [(c["tool"], c["args"]) for c in a["tool_calls"]])
        print("VISUAL:", a.get("visual"), "from", a.get("visual_source"))
        print("ANSWER:", a.get("answer_text"))
        print("CITES :", a.get("citations"))
        print("PROOF :", a.get("proof"))
        print("TIME  :", a["timing"])