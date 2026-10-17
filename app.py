import uuid
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import agent
import memory
import tools as t

st.set_page_config(page_title="Asset Intelligence Copilot", layout="wide")


# =============================================================== spec builder
# The model only chooses WHICH component. The data always comes from tool results.
def _rows_from(result):
    """Find the main list of records in a tool result."""
    if not isinstance(result, dict):
        return []
    for key in ("rows", "wells", "spills", "points", "cells", "regulator_events",
                "flagged", "turns"):
        v = result.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def _pick_call(out):
    calls = out.get("tool_calls", [])
    src = out.get("visual_source") or ""
    for c in calls:
        if c["tool"] == src:
            return c
    return calls[-1] if calls else None


def _flat(rows):
    """Flatten nested lists/dicts to text so tables always render."""
    clean = []
    for r in rows:
        clean.append({k: (v if isinstance(v, (str, int, float, bool)) or v is None else str(v))
                      for k, v in r.items()})
    return clean


def build_spec(out):
    visual = out.get("visual", "none")
    call = _pick_call(out)
    if visual == "none" or not call:
        return {"type": "none"}
    res = call["result"]
    rows = _rows_from(res)

    if visual == "status_card":
        for c in out.get("tool_calls", []):
            if c["tool"] == "well_status" and "latest_inspection" in c["result"]:
                return {"type": "status_card", "data": c["result"]}
        visual = "table"

    if visual == "line_chart":
        for c in out.get("tool_calls", []):
            if c["tool"] == "temperature_series":
                return {"type": "line_chart", "title": f"{c['result']['well_id']}: wellhead vs air temperature",
                        "points": c["result"]["points"]}
        if rows and len(rows[0]) >= 2:
            return {"type": "generic_line", "rows": _flat(rows)}
        visual = "table"

    if visual == "heatmap":
        for c in out.get("tool_calls", []):
            if c["tool"] == "spill_heatmap":
                return {"type": "heatmap", "title": f"Real spills by area and month, {c['result']['year']}",
                        "cells": c["result"]["cells"]}
        visual = "table"

    if visual == "map":
        pts = [r for r in rows if r.get("lat") is not None and r.get("lon") is not None]
        if pts:
            return {"type": "map", "title": "Locations", "points": _flat(pts)}
        visual = "table"

    if visual == "bar_chart":
        if rows:
            r0 = rows[0]
            label = next((k for k in ("well_id", "area", "name", "equipment_kind", "contractor") if k in r0),
                         next((k for k, v in r0.items() if isinstance(v, str)), None))
            num = next((k for k in ("downtime_days", "total_usd", "spill_count", "count", "barrels")
                        if k in r0), next((k for k, v in r0.items()
                                           if isinstance(v, (int, float)) and k != label), None))
            if label and num:
                return {"type": "bar_chart", "title": f"{num} by {label}", "label": label, "value": num,
                        "rows": _flat(rows)}
        visual = "table"

    if rows:
        return {"type": "table", "rows": _flat(rows)}
    return {"type": "none"}


# =============================================================== component registry
def render_status_card(spec, key):
    d = spec["data"]
    li = d["latest_inspection"]
    with st.container(border=True):
        c1, c2, c3 = st.columns(3)
        c1.metric(d["well_id"], "PRODUCING" if d["producing_now"] else "NOT PRODUCING")
        c2.metric("Above air", f"{li['above_air_c']} °C", f"limit {d['threshold_c']} °C", delta_color="off")
        c3.metric("Recorded status", d["recorded_status"],
                  "OUT OF DATE" if d["recorded_status_out_of_date"] else "matches", delta_color="off")
        st.caption(f"{d['type']} | {d['area']} | last inspected {li['at']} by {li['by']} ({li['method']}) | {li['id']}")
        if li.get("value_corrected"):
            st.write(f"Reading corrected: wellhead {li.get('original_wellhead_c')} °C (original) → {li['wellhead_c']} °C")
        if li.get("inspector_note"):
            st.write(f"Inspector note: {li['inspector_note']}")
        for n in d.get("recent_notes", [])[:3]:
            st.write(f"**{n['note_id']}** ({n['author']}, {n['note_type']}): {n['text']}")
        for n in d.get("shared_notes", [])[:3]:
            st.write(f"**NOTE-{n['id']}** ({n['author']}, {n['status']}): {n['text']}")
        for c in d.get("corrections", [])[:3]:
            st.write(f"**CORR-{c['id']}** by {c['author']}: {c['field']} {c['original_value']} → {c['new_value']} ({c['reason']})")
        for r in d.get("open_repairs", []):
            st.write(f"Open repair **{r['maintenance_id']}**: {r['issue']} (from {r['started_at'][:10]})")


def render_table(spec, key):
    st.dataframe(pd.DataFrame(spec["rows"]), use_container_width=True, hide_index=True)


def render_line(spec, key):
    df = pd.DataFrame(spec["points"])
    fig = go.Figure()
    fig.add_scatter(x=df["inspected_date"], y=df["wellhead_temp_c"], name="Wellhead °C", mode="lines+markers")
    fig.add_scatter(x=df["inspected_date"], y=df["ambient_temp_c"], name="Air °C", mode="lines+markers")
    down = df[df["is_producing"] == 0]
    if len(down):
        fig.add_scatter(x=down["inspected_date"], y=down["wellhead_temp_c"], name="Not producing",
                        mode="markers", marker=dict(color="red", size=14, symbol="x"))
    fig.update_layout(title=spec["title"], xaxis_title="Inspection date", yaxis_title="°C")
    st.plotly_chart(fig, use_container_width=True, key=key)


def render_generic_line(spec, key):
    df = pd.DataFrame(spec["rows"])
    st.plotly_chart(px.line(df, x=df.columns[0], y=list(df.columns[1:])), use_container_width=True, key=key)


def render_bar(spec, key):
    df = pd.DataFrame(spec["rows"])
    fig = px.bar(df, x=spec["label"], y=spec["value"], title=spec["title"], text=spec["value"])
    st.plotly_chart(fig, use_container_width=True, key=key)


def render_heatmap(spec, key):
    df = pd.DataFrame(spec["cells"])
    pv = df.pivot_table(index="area", columns="month", values="spills", aggfunc="sum", fill_value=0)
    pv = pv.reindex(columns=range(1, 13), fill_value=0)
    pv = pv.loc[:, (pv.sum() > 0) | (pv.columns <= int(df["month"].max()))]
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    pv.columns = [names[m - 1] for m in pv.columns]
    fig = px.imshow(pv, text_auto=True, aspect="auto", color_continuous_scale="Reds", title=spec["title"])
    st.plotly_chart(fig, use_container_width=True, key=key)


def render_map(spec, key):
    df = pd.DataFrame(spec["points"]).copy()
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"])
    if df.empty:
        st.info("No coordinates to plot.")
        return
    label = df.apply(lambda r: f"{r.get('well_id', '')} | {r.get('date', '')} | {r.get('barrels', '')} bbl", axis=1)
    kw = dict(lat=df["lat"], lon=df["lon"], mode="markers+text",
              marker=dict(size=16, color="red"),
              text=df["well_id"] if "well_id" in df else None,
              textposition="top right", hovertext=label, hoverinfo="text")
    center = dict(lat=float(df["lat"].mean()), lon=float(df["lon"].mean()))
    if hasattr(go, "Scattermap"):                     # newer Plotly
        fig = go.Figure(go.Scattermap(**kw))
        fig.update_layout(map=dict(style="open-street-map", center=center, zoom=11))
    else:                                             # older Plotly
        fig = go.Figure(go.Scattermapbox(**kw))
        fig.update_layout(mapbox=dict(style="open-street-map", center=center, zoom=11))
    fig.update_layout(margin=dict(l=0, r=0, t=30, b=0), height=450, title=spec["title"])
    st.plotly_chart(fig, use_container_width=True, key=key)


REGISTRY = {"status_card": render_status_card, "table": render_table, "line_chart": render_line,
            "generic_line": render_generic_line, "bar_chart": render_bar, "heatmap": render_heatmap,
            "map": render_map}


def render_spec(spec, key):
    fn = REGISTRY.get(spec.get("type"))
    if fn:
        try:
            fn(spec, key)
        except Exception as e:
            st.warning(f"Could not draw the {spec['type']} ({type(e).__name__}: {e}); showing the data instead.")
            if spec.get("rows"):
                render_table(spec, key)


# =============================================================== "how was this answered / verify" panel
def render_verify(confidence, checks, calls, timing, visual=None, proof=None):
    """calls: list of dicts with tool, args and (for run_sql) result rows."""
    if visual:
        st.write(f"Visual chosen: **{visual}**")
    st.write(f"**Confidence:** {confidence or 'n/a'}")
    for ch in checks or []:
        st.write("- " + ch)
    for p in proof or []:
        icon = "MATCH" if p.get("match") else ("MISMATCH" if p.get("match") is False else "n/a")
        st.write(f"**Independent proof ({icon}):** {p.get('check')}")
        st.write(f"App: {p.get('app')}  |  Raw files: {p.get('raw_files')}")
    for c in calls or []:
        st.code(f"{c['tool']}({c['args']})", language="python")
        res = c.get("result")
        if c["tool"] == "run_sql" and isinstance(res, dict):
            st.write(f"Rows returned: {res.get('row_count')}")
            if res.get("rows"):
                st.dataframe(_flat(res["rows"][:20]), hide_index=True)
    st.json(timing or {})


# =============================================================== session state
st.session_state.setdefault("chats", {})
st.session_state.setdefault("chat_ids", {})


# =============================================================== sidebar: user picker
@st.cache_data
def load_users():
    con = t.db()
    return [dict(r) for r in con.execute("SELECT user_id, name, role, team_id FROM users ORDER BY name")]


users = load_users()
with st.sidebar:
    st.header("User")
    label = lambda u: f"{u['name']} ({u['role']})"
    me = st.selectbox("Acting as", users, format_func=label)
    st.caption(f"{me['user_id']} | {me['team_id']}")
    if st.button("New chat"):
        st.session_state.chats[me["user_id"]] = []
        st.session_state.chat_ids[me["user_id"]] = str(uuid.uuid4())
    st.divider()
    st.caption("Examples")
    for ex in ["Is WELL-0001 producing oil right now?",
               "Show a line chart of WELL-0001 wellhead vs air temperature for the last 13 weeks",
               "Which 3 wells in Northeast Block 7 had the most downtime in the last 4 months? Show a bar chart.",
               "Show on a map every well in Northeast Block 8 with a real spill in the last 90 days.",
               "Show a heatmap of 2026 spills by geographic area and month.",
               "Which inspector found the most spills?"]:
        if st.button(ex, key=ex):
            st.session_state.pending = ex

chat_id = st.session_state.chat_ids.setdefault(me["user_id"], str(uuid.uuid4()))

with st.sidebar.expander("Recorded corrections, notes, rules"):
    act = memory.recent_activity()
    for c in act["corrections"]:
        st.write(f"**CORR-{c['id']}** {c['author']}: {c['scope_value']} {c['field']} "
                 f"{c['original_value']} → {c['new_value']} ({c['reason']})")
    for n in act["notes"]:
        st.write(f"**NOTE-{n['id']}** {n['author']} on {n['well_id']} [{n['status']}]: {n['text']}")
    for r in act["rules"]:
        st.write(f"**RULE-{r['id']}** {r['author']}: {r['scope_kind']} {r['scope_value']} "
                 f"{r['rule_key']} = {r['value']}")
    if not (act["corrections"] or act["notes"] or act["rules"]):
        st.caption("Nothing recorded yet.")

# =============================================================== chat
st.title("Asset Intelligence Copilot")
chat = st.session_state.chats.setdefault(me["user_id"], [])   # private per user

for i, m in enumerate(chat):
    with st.chat_message(m["role"]):
        st.write(m["text"])
        if m["role"] == "assistant":
            render_spec(m["spec"], key=f"{me['user_id']}-{chat_id}-{i}")
            if m.get("citations"):
                st.caption("Evidence: " + ", ".join(m["citations"][:25]))
            with st.expander("How this was answered / verify"):
                render_verify(m.get("confidence"), m.get("checks"), m.get("tool_calls"),
                              m.get("timing"), m.get("visual"), m.get("proof"))

q = st.chat_input("Ask about your wells")
if not q and "pending" in st.session_state:
    q = st.session_state.pop("pending")

if q:
    history = [{"role": m["role"], "content": m["text"]} for m in chat[-6:]]   # this user's own turns only
    chat.append({"role": "user", "text": q})
    with st.chat_message("user"):
        st.write(q)
    with st.chat_message("assistant"):
        with st.spinner("Working..."):
            out = agent.ask(q, history=history, user_id=me["user_id"])
        spec = build_spec(out)
        st.write(out.get("answer_text", ""))
        render_spec(spec, key=f"{me['user_id']}-{chat_id}-new-{len(chat)}")
        if out.get("citations"):
            st.caption("Evidence: " + ", ".join(out["citations"][:25]))
        with st.expander("How this was answered / verify"):
            render_verify(out.get("confidence"), out.get("checks"), out.get("tool_calls"),
                          out.get("timing"), out.get("visual"), out.get("proof"))
    memory.log_chat(me["user_id"], chat_id, "user", q, memory.wells_in(q))
    memory.log_chat(me["user_id"], chat_id, "assistant", out.get("answer_text", ""),
                    memory.wells_in(q, out.get("answer_text", "")))
    chat.append({"role": "assistant", "text": out.get("answer_text", ""), "spec": spec,
                 "visual": out.get("visual"), "citations": out.get("citations", []),
                 "confidence": out.get("confidence"), "checks": out.get("checks", []),
                 "proof": out.get("proof", []),
                 "tool_calls": [{"tool": c["tool"], "args": c["args"],
                                 "result": ({"row_count": c["result"].get("row_count"),
                                             "rows": c["result"].get("rows", [])[:20]}
                                            if c["tool"] == "run_sql" and isinstance(c["result"], dict) else None)}
                                for c in out.get("tool_calls", [])],
                 "timing": out.get("timing", {})})