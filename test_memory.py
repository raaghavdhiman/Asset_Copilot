import os, sys, time, sqlite3
import agent
import memory

# Wipe memory ONLY when asked (first slice). Later slices need earlier corrections.
if os.getenv("FRESH") == "1" and os.path.exists("memory.db"):
    os.remove("memory.db")

con = sqlite3.connect("copilot.db")
U = {n: i for i, n in con.execute("SELECT user_id, name FROM users")}

steps = [
 ("Michael Turner", "Which 3 wells in Northeast Block 7 had the most downtime in the last 4 months? Show a bar chart.", "Q6: 0121 49, 0138 42, 0107 42"),
 ("Michael Turner", "For the three wells we discussed yesterday, what have repairs cost this year?", "Q11: 0121 25794, 0138 46942, 0107 5176"),
 ("Michael Turner", "The 15 Oct reading for WELL-0164 is wrong. The handheld thermal gun used in that inspection was faulty; the field crew re-measured the wellhead at 41.0 C.", "Q12: correction saved, producing, flags 'Pump jack not moving' conflict, asks to confirm"),
 ("Michael Turner", "Which wells in Northeast Block 8 are not producing oil right now, and why? Show them in a table.", "Q12: 0170, 0172, 0175 only; says 0164 removed by correction"),
 ("Michael Turner", "The AI estimated the 24 Sep spill at WELL-0151 as 4.5 bbl. The field crew measured 5.4 bbl.", "Q13: reportable, no report on record (SR-01), report class stale, SR-03 due 2026-11-23"),
 ("Jessica Reed", "Is WELL-0164 producing oil right now?", "Q15: yes, corrected by Michael Turner, mentions inspector note"),
 ("Laura Simmons", "Which spills in Northeast Block 8 in the last 90 days needed a report to the regulator?", "Q15: WELL-0151 only, overdue"),
 ("Michael Turner", "Which wells in Northeast Block 7 need a root-cause analysis on their pump jack?", "Q14 before: 0108, 0121, 0138"),
 ("Michael Turner", "Note for all T350 wells: our team now wants a pump jack root-cause analysis after 2 repairs, not 3.", "Q14: rule saved"),
 ("Michael Turner", "Which wells in Northeast Block 7 need a root-cause analysis on their pump jack?", "Q14 after: 0108, 0120, 0121, 0138; 0102 (T200) NOT added; cites Michael's rule"),
 ("Jake Morales", "FYI for everyone: WELL-0007 will be shut in from 17 Oct for a 10-day planned workover.", "Q16: note saved"),
 ("Michael Turner", "Is any downtime planned for WELL-0007?", "Q16: yes, from 17 Oct ~10 days, per Jake Morales"),
 ("Jessica Reed", "For the three wells we discussed yesterday, what have repairs cost this year?", "Q16: no earlier conversation for Jessica"),
 ("Michael Turner", "WELL-0175 has been shut in for planned work since 14 Oct.", "Q17: consistent with 15 Oct inspection"),
 ("Tyler Hughes", "WELL-0175 is running fine.", "Q17: conflicts, 0.4 C above air, unconfirmed, asks to confirm"),
 ("Jake Morales", "WELL-0001 is back online since 14 Oct.", "Q17: conflicts with 15 Oct inspection and CTX-0001-003, unconfirmed"),
 ("Laura Simmons", "Was the 20 Aug spill at WELL-0101 real oil?", "Q18: yes, shows Ryan Brooks vs Chris Dalton disagreement"),
]

a_ = int(sys.argv[1]) if len(sys.argv) > 1 else 0
b_ = int(sys.argv[2]) if len(sys.argv) > 2 else len(steps)

for who, q, expect in steps[a_:b_]:
    print("\n" + "=" * 72, f"\n[{who}] {q}\nEXPECT: {expect}")
    a = agent.ask(q, user_id=U[who])
    print("TOOLS :", [c["tool"] for c in a["tool_calls"]])
    print("ANSWER:", a.get("answer_text"))
    print("TIME  :", a["timing"]["total_s"])
    memory.log_chat(U[who], "test", "user", q, memory.wells_in(q))
    memory.log_chat(U[who], "test", "assistant", a.get("answer_text", ""),
                    memory.wells_in(q, a.get("answer_text", "")))
    time.sleep(30)