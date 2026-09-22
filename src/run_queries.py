"""
Runs every labeled query in sql/saal_analysis.sql against data/saas_audit.db
and writes each result to a CSV in sql_results/ (ready to point Power BI at).

Usage:
    python sql/run_queries.py
"""

import os
import sqlite3

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE, "data", "saas_audit.db")
SQL_PATH = os.path.join(BASE, "sql", "saal_analysis.sql")
OUT_DIR = os.path.join(BASE, "sql_results")
os.makedirs(OUT_DIR, exist_ok=True)

LABEL_MARKERS = {
    "Q1.": "Q1_trusted_vs_reported_mrr",
    "Q2.": "Q2_high_rev_low_usage_high_escalation",
    "Q3.": "Q3_discount_vs_retention",
    "4a.": "Q4a_revenue_at_risk_by_region",
    "4b.": "Q4b_revenue_at_risk_by_plan",
    "4c.": "Q4c_revenue_at_risk_by_channel",
    "Q5.": "Q5_contradictions",
    "Q6.": "Q6_executive_summary",
}


def main():
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"{DB_PATH} not found — run src/build_db.py first.")

    with open(SQL_PATH) as f:
        sql_text = f.read()

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    current_label = None
    out_frames = {}

    for chunk in sql_text.split(";"):
        if not chunk.strip():
            continue

        for marker, label in LABEL_MARKERS.items():
            if marker in chunk:
                current_label = label

        # skip pure-comment chunks
        if all(ln.strip().startswith("--") or not ln.strip() for ln in chunk.split("\n")):
            continue

        try:
            if chunk.strip().upper().startswith(("DROP", "CREATE")):
                cur.execute(chunk)
                con.commit()
                continue
            df = pd.read_sql_query(chunk, con)
            key = current_label or f"stmt_{len(out_frames) + 1}"
            base_key, i = key, 1
            while key in out_frames:
                i += 1
                key = f"{base_key}_{i}"
            out_frames[key] = df
        except Exception as e:
            print(f"Skipped a statement ({e})")

    for name, df in out_frames.items():
        path = os.path.join(OUT_DIR, f"{name}.csv")
        df.to_csv(path, index=False)
        print(f"{name}: {df.shape[0]} rows -> {path}")

    con.close()


if __name__ == "__main__":
    main()
