import sqlite3
con = sqlite3.connect("copilot.db")
con.execute("DROP TABLE IF EXISTS docs_fts")
con.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(source, kind UNINDEXED, well_id UNINDEXED, well_type UNINDEXED, text)")
for doc, wt, section, text in con.execute("SELECT doc, well_type, section, text FROM docs").fetchall():
    con.execute("INSERT INTO docs_fts VALUES (?,?,?,?,?)", (f"{doc} / {section}", "manual", "", wt or "", text))
for mid, wid, text in con.execute("SELECT maintenance_id, well_id, text FROM reports").fetchall():
    wt = con.execute("SELECT well_type FROM wells WHERE well_id=?", (wid,)).fetchone()[0]
    con.execute("INSERT INTO docs_fts VALUES (?,?,?,?,?)", (mid, "report", wid, wt or "", text))
con.commit()
print("indexed:", con.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0])