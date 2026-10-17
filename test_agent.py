import time
import agent

questions = [
    "Is WELL-0001 producing oil right now? When was it last inspected, and by whom?",
    "Which wells in Northeast Block 8 are not producing oil right now, and why? Show them in a table.",
    "How many oil spills has WELL-0001 had in the last 4 years, and how much oil in total? List them in a table.",
    "Which 3 wells in Northeast Block 7 had the most downtime in the last 4 months? Show a bar chart.",
    "In Southwest Block 24, which spills in the last 90 days were not contained within the SOP time limit?",
    "What does the Spill Response SOP say about clean-up verification?",
    # out-of-the-box questions: no ready-made tool covers these, so we expect run_sql
    "Which inspector found the most spills?",
    "What was the average repair cost by equipment kind in 2025?",
    "Which contractor did the most corrective repairs?",
]

for q in questions:
    print("\n" + "=" * 70 + "\nQ:", q)
    try:
        a = agent.ask(q)
        print("TOOLS :", [(c["tool"], c["args"]) for c in a["tool_calls"]])
        for c in a["tool_calls"]:
            if c["tool"] == "run_sql":
                print("SQL   :", c["result"].get("sql") or c["result"].get("error"))
        print("VISUAL:", a.get("visual"), "from", a.get("visual_source"))
        print("ANSWER:", a.get("answer_text"))
        print("CITES :", a.get("citations"))
    except Exception as e:
        print("FAILED:", type(e).__name__, str(e)[:200])
    time.sleep(10)