# Asset Intelligence Copilot

Chat assistant for oil-well data: exact numbers from code/SQL, documents via full-text search,
two-level memory (shared notes/corrections, private per-user history), generative UI.

## Run
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and add a Groq key.
3. Put the dataset in `test-data/` (needs `fleet/` and `assets/`).
4. `python loader.py test-data copilot.db`
5. `python build_index.py`
6. `streamlit run app.py`

## Design
- Data layer: SQLite; producing threshold parsed from manuals; downtime, real spills precomputed.
- Tools: tested Python functions return answers plus record IDs; `run_sql` and `search_docs` for unseen questions.
- Two-phase agent: model picks tools, then writes the answer; citations filtered to real IDs.
- Memory: `memory.db` (corrections by scope, shared notes, rules, per-user chat log).
- Proof: `prove.py` recomputes answers from raw files.
