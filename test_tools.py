import tools as t

def show(title, expect, got):
    print(f"\n{title}\n  expect: {expect}\n  got   : {got}")

s = t.well_status("WELL-0001")
show("Q1", "not producing, INS-0001-0203, Ryan Brooks, 3.2, status out of date, note CTX-0001-003",
     (s["producing_now"], s["latest_inspection"]["id"], s["latest_inspection"]["by"],
      s["latest_inspection"]["above_air_c"], s["recorded_status_out_of_date"],
      [n["note_id"] for n in s["recent_notes"]]))

r = t.not_producing_now("Northeast Block 8")
show("Q2", "164 (4.1, no reason), 170 (2.9, MNT-0170-031), 172 (5.7, MNT-0172-028), 175 (0.4, no reason)",
     [(w["well_id"], w["above_air_c"], w["has_recorded_reason"],
       [x["maintenance_id"] for x in w["open_repairs"]]) for w in r["wells"]])

p = t.temperature_series("WELL-0001", 13)["points"]
show("Q3", "13 points, 23 Jul to 15 Oct, not producing on 2026-10-08 and 2026-10-15",
     (len(p), p[0]["inspected_date"], p[-1]["inspected_date"],
      [x["inspected_date"] for x in p if x["is_producing"] == 0]))

sp = t.spills("WELL-0001", "2022-10-01", t.TODAY)
show("Q4", "6 spills, 15.1 bbl", (sp["count"], sp["total_bbl"]))

d = t.downtime("WELL-0001", "2025-01-01", "2025-12-31")
show("Q5", "21 days", d["wells"][0]["downtime_days"])

d = t.downtime("Northeast Block 7", "2026-06-16", "2026-10-16", top_n=4)
show("Q6", "0121 49, 0138 42, 0107 42, 0119 28", [(w["well_id"], w["downtime_days"]) for w in d["wells"]])

m = t.spills("Northeast Block 8", t.days_ago(90), t.TODAY)
show("Q7", "0151 4.5, 0165 1.2, 0175 3.9, 0186 0.6", [(x["well_id"], x["date"], x["barrels"]) for x in m["spills"]])

h = t.spill_heatmap(2026)["cells"]
tot = {}
for c in h:
    tot[c["area"]] = tot.get(c["area"], 0) + c["spills"]
show("Q8", "SW23 35, SW24 43, NE7 37, NE8 26", tot)

rc = t.rule_check("WELL-0001", t.days_ago(365), t.TODAY)
show("Q9", "SR-01 (41 h) and SR-02 (7 h) computed; events REG-0001-001/002/003 listed",
     ([(b["rule"], b["detail"]) for b in rc["computed_breaches"]],
      [(e["event_id"], e["fine_usd"], e["status"]) for e in rc["regulator_events"]]))

q = t.spills("Southwest Block 24", t.days_ago(90), t.TODAY, with_containment=True)
show("Q10", "WELL-0082 7.1 and WELL-0100 8.1",
     [(x["well_id"], x["containment_hours"]) for x in q["spills"] if x["breaches_sr02"]])

c = t.repair_costs(["WELL-0121", "WELL-0138", "WELL-0107"], 2026)
show("Q11", "0121 25794, 0138 46942, 0107 5176", [(x["well_id"], x["total_usd"]) for x in c["wells"]])