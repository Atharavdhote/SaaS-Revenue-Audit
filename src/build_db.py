"""
Builds data/saas_audit.db from:
  - the raw source workbook in data/raw/  (for the "Reported MRR" figures)
  - the cleaned CSVs in data/cleaned/     (produced by SaaSRevenueAuditEngine.py)

Usage:
    python src/build_db.py
"""

import glob
import os
import sqlite3
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(BASE, "data", "raw")
CLEAN_DIR = os.path.join(BASE, "data", "cleaned")
DB_PATH = os.path.join(BASE, "data", "saas_audit.db")


def find_raw_workbook():
    candidates = glob.glob(os.path.join(RAW_DIR, "*.xlsx"))
    if not candidates:
        sys.exit(f"No .xlsx file found in {RAW_DIR}")
    if len(candidates) > 1:
        print(f"Multiple .xlsx files found in {RAW_DIR}, using: {candidates[0]}")
    return candidates[0]


def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)

    raw_path = find_raw_workbook()
    print(f"Loading raw subscriptions from: {raw_path}")
    raw_subs = pd.read_excel(raw_path, sheet_name="subscriptions")
    raw_subs.to_sql("raw_subscriptions", con, if_exists="replace", index=False)

    required = ["subscriptions", "invoices", "product_usage", "support_cases"]
    for name in required:
        path = os.path.join(CLEAN_DIR, f"clean_{name}.csv")
        if not os.path.exists(path):
            sys.exit(f"Missing {path} — run SaaSRevenueAuditEngine.py first.")
        df = pd.read_csv(path)
        df.to_sql(f"clean_{name}", con, if_exists="replace", index=False)
        print(f"Loaded clean_{name}: {len(df)} rows")

    summary_path = os.path.join(CLEAN_DIR, "customer_summary.csv")
    if not os.path.exists(summary_path):
        sys.exit(f"Missing {summary_path} — run SaaSRevenueAuditEngine.py first.")
    customer_summary = pd.read_csv(summary_path)
    customer_summary.to_sql("customer_summary", con, if_exists="replace", index=False)
    print(f"Loaded customer_summary: {len(customer_summary)} rows")

    con.commit()
    con.close()
    print(f"\nDatabase built at: {DB_PATH}")
    print("Tables: raw_subscriptions, clean_subscriptions, clean_invoices, "
          "clean_product_usage, clean_support_cases, customer_summary")


if __name__ == "__main__":
    main()
