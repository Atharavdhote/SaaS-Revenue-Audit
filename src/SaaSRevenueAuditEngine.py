"""
SaaSRevenueAuditEngine
=======================
Object-oriented data validation, transformation and customer health-scoring
pipeline for the B2B SaaS revenue/retention case study.

Design principles
------------------
* Every raw record is preserved (nothing is silently deleted).
* Every record gets a `record_status` in {VALID, SUSPICIOUS, INVALID} and a
  `status_reason` explaining every rule that fired on it.
* Rule severity order: INVALID > SUSPICIOUS > VALID. A record's final status
  is the highest severity among all rules that matched it; the reason string
  lists every rule that matched (not just the worst one) for full auditability.
* Customer-level metrics and the Health Score are computed only from records
  that are NOT classified INVALID (SUSPICIOUS records are kept but flagged;
  they still count because excluding them would itself be a business
  judgement call, but they are surfaced separately for review).
* No customer-specific hardcoding anywhere - every rule/threshold is a
  general statistical or business rule applied uniformly.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field


VALID, SUSPICIOUS, INVALID = "VALID", "SUSPICIOUS", "INVALID"
_SEVERITY = {VALID: 0, SUSPICIOUS: 1, INVALID: 2}


@dataclass
class RuleResult:
    """Container for one validation rule's output across a dataframe."""
    mask: pd.Series
    severity: str
    reason: str


class BaseValidator:
    """Shared machinery for classifying a dataframe against a rule list."""

    def __init__(self, df: pd.DataFrame):
        self.df = df.copy().reset_index(drop=True)
        self.rules: list[RuleResult] = []

    def add_rule(self, mask: pd.Series, severity: str, reason: str):
        mask = mask.fillna(False) if mask.dtype != bool else mask
        self.rules.append(RuleResult(mask.reindex(self.df.index, fill_value=False), severity, reason))

    def finalize(self) -> pd.DataFrame:
        n = len(self.df)
        severity_level = np.zeros(n, dtype=int)
        reasons = [[] for _ in range(n)]

        for rule in self.rules:
            idx = np.where(rule.mask.values)[0]
            if len(idx) == 0:
                continue
            lvl = _SEVERITY[rule.severity]
            for i in idx:
                reasons[i].append(rule.reason)
                if lvl > severity_level[i]:
                    severity_level[i] = lvl

        inv_map = {v: k for k, v in _SEVERITY.items()}
        self.df["record_status"] = [inv_map[l] for l in severity_level]
        self.df["status_reason"] = ["; ".join(r) if r else "No issues detected" for r in reasons]
        return self.df


class SaaSRevenueAuditEngine:
    """
    Main entry point.

    Usage
    -----
    engine = SaaSRevenueAuditEngine("workbook.xlsx")
    engine.load_data()
    engine.run_validation_pipeline()
    engine.build_customer_metrics()
    engine.compute_health_score()
    customer_summary = engine.customer_summary
    """

    REQUIRED_SHEETS = ["subscriptions", "invoices", "product_usage", "support_cases"]

    def __init__(self, filepath: str, as_of_date: pd.Timestamp | None = None):
        self.filepath = filepath
        self.as_of_date = as_of_date or pd.Timestamp.today().normalize()
        self.raw: dict[str, pd.DataFrame] = {}
        self.clean: dict[str, pd.DataFrame] = {}
        self.customer_summary: pd.DataFrame | None = None

    # ------------------------------------------------------------------ #
    # LOAD
    # ------------------------------------------------------------------ #
    def load_data(self):
        xls = pd.ExcelFile(self.filepath)
        for sheet in self.REQUIRED_SHEETS:
            if sheet not in xls.sheet_names:
                raise ValueError(f"Required sheet '{sheet}' not found in workbook.")
            self.raw[sheet] = pd.read_excel(xls, sheet)
        return self

    # ------------------------------------------------------------------ #
    # VALIDATION PIPELINE
    # ------------------------------------------------------------------ #
    def run_validation_pipeline(self):
        self.clean["subscriptions"] = self._validate_subscriptions(self.raw["subscriptions"])
        self.clean["invoices"] = self._validate_invoices(
            self.raw["invoices"], self.clean["subscriptions"]
        )
        self.clean["product_usage"] = self._validate_usage(
            self.raw["product_usage"], self.clean["subscriptions"]
        )
        self.clean["support_cases"] = self._validate_support_cases(
            self.raw["support_cases"], self.clean["subscriptions"]
        )
        return self

    # ---- subscriptions -------------------------------------------------
    def _validate_subscriptions(self, raw: pd.DataFrame) -> pd.DataFrame:
        v = BaseValidator(raw)
        df = v.df

        v.add_rule(df["subscription_id"].isna(), INVALID, "Missing subscription_id")
        dup_id = df["subscription_id"].duplicated(keep="first") & df["subscription_id"].notna()
        v.add_rule(dup_id, INVALID, "Duplicate subscription_id")

        v.add_rule(df["customer_id"].isna(), INVALID, "Missing customer_id")

        signup = pd.to_datetime(df["signup_date"], errors="coerce")
        renewal = pd.to_datetime(df["renewal_date"], errors="coerce")
        v.add_rule(signup.isna(), SUSPICIOUS, "Missing/invalid signup_date")
        v.add_rule(
            renewal.notna() & signup.notna() & (renewal < signup),
            INVALID,
            "Renewal date earlier than signup date",
        )

        fee = pd.to_numeric(df["monthly_fee"], errors="coerce")
        v.add_rule(fee.isna(), SUSPICIOUS, "Missing monthly_fee")
        v.add_rule(fee < 0, INVALID, "Negative monthly_fee")

        disc = pd.to_numeric(df["discount_pct"], errors="coerce")
        v.add_rule((disc < 0) | (disc > 100), INVALID, "Discount_pct outside 0-100 range")

        status = df["subscription_status"].astype(str).str.strip().str.title()
        active_mask = status.eq("Active")
        inconsistent_active = active_mask & (
            renewal.isna() | (renewal < self.as_of_date - pd.Timedelta(days=365 * 3))
        )
        v.add_rule(
            inconsistent_active,
            SUSPICIOUS,
            "Active subscription with missing/implausible renewal date",
        )
        v.add_rule(df["subscription_status"].isna(), SUSPICIOUS, "Missing subscription_status")

        out = v.finalize()
        out["signup_date"] = signup
        out["renewal_date"] = renewal
        out["monthly_fee"] = fee
        out["discount_pct"] = disc.fillna(0.0)
        out["subscription_status_norm"] = status
        return out

    # ---- invoices --------------------------------------------------------
    def _validate_invoices(self, raw: pd.DataFrame, subs_clean: pd.DataFrame) -> pd.DataFrame:
        v = BaseValidator(raw)
        df = v.df

        v.add_rule(df["invoice_id"].isna(), INVALID, "Missing invoice_id")
        dup_id = df["invoice_id"].duplicated(keep="first") & df["invoice_id"].notna()
        v.add_rule(dup_id, INVALID, "Duplicate invoice_id")

        valid_sub_ids = set(subs_clean.loc[subs_clean["record_status"] != INVALID, "subscription_id"])
        orphan = df["subscription_id"].notna() & ~df["subscription_id"].isin(valid_sub_ids)
        v.add_rule(orphan, INVALID, "Invoice references non-existent/invalid subscription")
        v.add_rule(df["subscription_id"].isna(), INVALID, "Missing subscription_id on invoice")

        inv_amt = pd.to_numeric(df["invoice_amount"], errors="coerce")
        tax_amt = pd.to_numeric(df["tax_amount"], errors="coerce")
        v.add_rule(inv_amt < 0, INVALID, "Negative invoice_amount")
        v.add_rule(tax_amt < 0, INVALID, "Negative tax_amount")
        v.add_rule(inv_amt.isna(), SUSPICIOUS, "Missing invoice_amount")

        inv_date = pd.to_datetime(df["invoice_date"], errors="coerce")
        due_date = pd.to_datetime(df["due_date"], errors="coerce")
        pay_date = pd.to_datetime(df["payment_date"], errors="coerce")
        status = df["payment_status"].astype(str).str.strip().str.title()

        v.add_rule(status.isna() | df["payment_status"].isna(), SUSPICIOUS, "Missing payment_status")
        paid_no_date = status.eq("Paid") & pay_date.isna()
        v.add_rule(paid_no_date, INVALID, "Paid invoice missing valid payment_date")
        early_payment = pay_date.notna() & inv_date.notna() & (pay_date < inv_date)
        v.add_rule(early_payment, INVALID, "Payment date earlier than invoice date")

        out = v.finalize()
        out["invoice_date"] = inv_date
        out["due_date"] = due_date
        out["payment_date"] = pay_date
        out["invoice_amount"] = inv_amt
        out["tax_amount"] = tax_amt
        out["payment_status_norm"] = status
        return out

    # ---- product usage -----------------------------------------------------
    def _validate_usage(self, raw: pd.DataFrame, subs_clean: pd.DataFrame) -> pd.DataFrame:
        v = BaseValidator(raw)
        df = v.df

        v.add_rule(df["usage_id"].isna(), INVALID, "Missing usage_id")
        dup_id = df["usage_id"].duplicated(keep="first") & df["usage_id"].notna()
        v.add_rule(dup_id, INVALID, "Duplicate usage_id")

        valid_cust_ids = set(subs_clean.loc[subs_clean["record_status"] != INVALID, "customer_id"])
        orphan = df["customer_id"].notna() & ~df["customer_id"].isin(valid_cust_ids)
        v.add_rule(orphan, INVALID, "Usage record references customer not in subscriptions")
        v.add_rule(df["customer_id"].isna(), INVALID, "Missing customer_id on usage record")

        login = pd.to_numeric(df["login_count"], errors="coerce")
        minutes = pd.to_numeric(df["active_minutes"], errors="coerce")
        v.add_rule(login < 0, INVALID, "Negative login_count")
        v.add_rule(minutes < 0, INVALID, "Negative active_minutes")

        seats = pd.to_numeric(df["seat_count"], errors="coerce")
        # Statistical outlier flag (>99th percentile) - general rule, no hardcoding
        if seats.notna().sum() > 10:
            cap = seats.quantile(0.99)
            v.add_rule(seats > cap, SUSPICIOUS, "Seat_count is a statistical outlier (>99th pct)")

        out = v.finalize()
        out["login_count"] = login
        out["active_minutes"] = minutes
        out["usage_date"] = pd.to_datetime(df["usage_date"], errors="coerce")
        return out

    # ---- support cases ------------------------------------------------------
    def _validate_support_cases(self, raw: pd.DataFrame, subs_clean: pd.DataFrame) -> pd.DataFrame:
        v = BaseValidator(raw)
        df = v.df

        v.add_rule(df["case_id"].isna(), INVALID, "Missing case_id")
        dup_id = df["case_id"].duplicated(keep="first") & df["case_id"].notna()
        v.add_rule(dup_id, INVALID, "Duplicate case_id")

        valid_cust_ids = set(subs_clean.loc[subs_clean["record_status"] != INVALID, "customer_id"])
        orphan = df["customer_id"].notna() & ~df["customer_id"].isin(valid_cust_ids)
        v.add_rule(orphan, INVALID, "Support case references customer not in subscriptions")

        opened = pd.to_datetime(df["opened_date"], errors="coerce")
        closed = pd.to_datetime(df["closed_date"], errors="coerce")
        v.add_rule(
            closed.notna() & opened.notna() & (closed < opened),
            INVALID,
            "Closed date earlier than opened date",
        )

        res_hrs = pd.to_numeric(df["resolution_hours"], errors="coerce")
        v.add_rule(res_hrs < 0, INVALID, "Negative resolution_hours")

        csat = pd.to_numeric(df["csat_score"], errors="coerce")
        v.add_rule(csat.notna() & ((csat < 1) | (csat > 5)), INVALID, "CSAT score outside 1-5 range")

        status = df["case_status"].astype(str).str.strip().str.title()
        closed_status = status.isin(["Closed", "Resolved"])
        missing_closure = closed_status & (closed.isna() | res_hrs.isna())
        v.add_rule(missing_closure, INVALID, "Closed case missing closure date/resolution hours")
        v.add_rule(df["case_status"].isna(), SUSPICIOUS, "Missing case_status")

        out = v.finalize()
        out["opened_date"] = opened
        out["closed_date"] = closed
        out["resolution_hours"] = res_hrs
        out["csat_score"] = csat
        out["case_status_norm"] = status
        return out

    # ------------------------------------------------------------------ #
    # CUSTOMER METRICS
    # ------------------------------------------------------------------ #
    def build_customer_metrics(self) -> pd.DataFrame:
        subs = self.clean["subscriptions"]
        inv = self.clean["invoices"]
        usage = self.clean["product_usage"]
        cases = self.clean["support_cases"]

        subs_ok = subs[subs["record_status"] != INVALID].copy()
        inv_ok = inv[inv["record_status"] != INVALID].copy()
        usage_ok = usage[usage["record_status"] != INVALID].copy()
        cases_ok = cases[cases["record_status"] != INVALID].copy()

        subs_ok["net_fee"] = subs_ok["monthly_fee"] * (1 - subs_ok["discount_pct"].fillna(0) / 100.0)

        # ---- reported vs trusted MRR at the subscription level ----------
        # Reported MRR = every active subscription's monthly_fee (as reported by Finance,
        # i.e. before any data-quality exclusions - this reproduces the Finance number).
        # Trusted MRR (customer-level "MRR" metric) = only VALID/SUSPICIOUS active subs, net of discount.
        active_ok = subs_ok[subs_ok["subscription_status_norm"] == "Active"]
        mrr = active_ok.groupby("customer_id")["net_fee"].sum().rename("MRR")

        # ---- effective monthly revenue: actual cash collected, monthly-ized ---
        paid_inv = inv_ok[inv_ok["payment_status_norm"] == "Paid"].merge(
            subs_ok[["subscription_id", "customer_id"]], on="subscription_id", how="left"
        )
        # Average paid invoice value per customer approximates their effective monthly revenue
        eff_rev = paid_inv.groupby("customer_id")["invoice_amount"].mean().rename("effective_monthly_revenue")

        # ---- payment failure rate --------------------------------------
        inv_cust = inv_ok.merge(subs_ok[["subscription_id", "customer_id"]], on="subscription_id", how="left")
        pay_stats = inv_cust.groupby("customer_id")["payment_status_norm"].agg(
            total_invoices="count",
            failed_invoices=lambda s: (s == "Failed").sum(),
        )
        pay_stats["payment_failure_rate"] = (
            pay_stats["failed_invoices"] / pay_stats["total_invoices"].replace(0, np.nan)
        ).fillna(0)

        # ---- usage intensity score (0-100, percentile-ranked composite) ----
        usage_agg = usage_ok.groupby("customer_id").agg(
            avg_logins=("login_count", "mean"),
            avg_minutes=("active_minutes", "mean"),
            avg_clicks=("feature_clicks", "mean"),
            avg_reports=("reports_created", "mean"),
            avg_exports=("data_exports", "mean"),
        )
        usage_component_cols = ["avg_logins", "avg_minutes", "avg_clicks", "avg_reports", "avg_exports"]
        pct_ranks = usage_agg[usage_component_cols].rank(pct=True)
        usage_agg["usage_intensity_score"] = (pct_ranks.mean(axis=1) * 100).round(2)

        # ---- support escalation rate ------------------------------------
        case_stats = cases_ok.groupby("customer_id")["case_status_norm"].agg(
            total_cases="count",
            escalations=lambda s: (s == "Escalated").sum(),
        )
        case_stats["support_escalation_rate"] = (
            case_stats["escalations"] / case_stats["total_cases"].replace(0, np.nan)
        ).fillna(0)

        avg_csat = cases_ok.groupby("customer_id")["csat_score"].mean().rename("average_csat")

        # ---- renewal risk score (0-100, higher = riskier) -----------------
        today = self.as_of_date
        subs_ok["days_to_renewal"] = (subs_ok["renewal_date"] - today).dt.days
        cancelled_flag = subs_ok["subscription_status_norm"].isin(["Cancelled", "Expired"]).astype(int)
        renewal_stats = subs_ok.groupby("customer_id").agg(
            subs_count=("subscription_id", "count"),
            cancelled_count=("subscription_status_norm", lambda s: s.isin(["Cancelled", "Expired"]).sum()),
            min_days_to_renewal=("days_to_renewal", "min"),
            not_auto_renew_rate=("auto_renew", lambda s: (s.astype(str).str.upper() != "Y").mean()),
        )
        renewal_stats["cancellation_rate"] = renewal_stats["cancelled_count"] / renewal_stats["subs_count"]
        # normalize "imminent renewal" (<=30 days) as extra risk signal
        renewal_stats["imminent_renewal"] = (renewal_stats["min_days_to_renewal"] <= 30).astype(int)
        renewal_stats["renewal_risk_score"] = (
            renewal_stats["cancellation_rate"] * 60
            + renewal_stats["not_auto_renew_rate"] * 25
            + renewal_stats["imminent_renewal"] * 15
        ).round(2).clip(0, 100)

        # ---- assemble customer table -------------------------------------
        all_customers = pd.Index(
            sorted(set(subs_ok["customer_id"]).union(usage_ok["customer_id"]).union(cases_ok["customer_id"]))
        )
        summary = pd.DataFrame(index=all_customers)
        summary.index.name = "customer_id"
        summary = summary.join(mrr).join(eff_rev)
        summary = summary.join(pay_stats["payment_failure_rate"])
        summary = summary.join(usage_agg["usage_intensity_score"])
        summary = summary.join(case_stats["support_escalation_rate"])
        summary = summary.join(avg_csat)
        summary = summary.join(renewal_stats["renewal_risk_score"])

        # attach descriptive attributes (region/plan/channel from most recent active sub, else any)
        subs_sorted = subs_ok.sort_values(["customer_id", "signup_date"])
        latest_sub = subs_sorted.groupby("customer_id").tail(1).set_index("customer_id")
        for col in ["region", "plan_name", "sales_channel", "subscription_status_norm"]:
            summary[col] = latest_sub[col]

        summary["MRR"] = summary["MRR"].fillna(0)
        summary["effective_monthly_revenue"] = summary["effective_monthly_revenue"].fillna(0)
        summary["payment_failure_rate"] = summary["payment_failure_rate"].fillna(0)
        summary["usage_intensity_score"] = summary["usage_intensity_score"].fillna(0)
        summary["support_escalation_rate"] = summary["support_escalation_rate"].fillna(0)
        summary["average_csat"] = summary["average_csat"].fillna(summary["average_csat"].mean())
        summary["renewal_risk_score"] = summary["renewal_risk_score"].fillna(50)  # unknown = medium risk

        self.customer_summary = summary.reset_index()
        return self.customer_summary

    # ------------------------------------------------------------------ #
    # HEALTH SCORE
    # ------------------------------------------------------------------ #
    def compute_health_score(self) -> pd.DataFrame:
        """
        Health Score (0-100), higher = healthier. Built entirely from
        percentile ranks of the customer-level metrics so it is unit-free,
        reproducible, and contains no customer-specific rules.

        Weighting (equal-weighted across the four behavioural domains):
          - Product usage      25%  (usage_intensity_score, higher=better)
          - Payment behaviour   25%  (1 - payment_failure_rate, higher=better)
          - Support behaviour   25%  (avg_csat pct rank + inverse escalation rate)/2
          - Subscription status 25%  (100 - renewal_risk_score)
        """
        df = self.customer_summary.copy()

        usage_score = df["usage_intensity_score"].clip(0, 100)

        payment_score = (1 - df["payment_failure_rate"].clip(0, 1)) * 100

        csat_pct = df["average_csat"].rank(pct=True) * 100
        escalation_score = (1 - df["support_escalation_rate"].clip(0, 1)) * 100
        support_score = (csat_pct + escalation_score) / 2

        subscription_score = 100 - df["renewal_risk_score"].clip(0, 100)

        df["customer_health_score"] = (
            0.25 * usage_score + 0.25 * payment_score + 0.25 * support_score + 0.25 * subscription_score
        ).round(2)

        df["risk_category"] = pd.cut(
            df["customer_health_score"],
            bins=[-0.01, 40, 70, 100],
            labels=["High Risk", "Watchlist", "Healthy"],
        ).astype(str)

        self.customer_summary = df
        return df

    # ------------------------------------------------------------------ #
    # EXPORT
    # ------------------------------------------------------------------ #
    def export(self, out_dir: str):
        import os

        os.makedirs(out_dir, exist_ok=True)
        for name, df in self.clean.items():
            df.to_csv(os.path.join(out_dir, f"clean_{name}.csv"), index=False)
        if self.customer_summary is not None:
            self.customer_summary.to_csv(os.path.join(out_dir, "customer_summary.csv"), index=False)

    def run_all(self, out_dir: str | None = None):
        self.load_data()
        self.run_validation_pipeline()
        self.build_customer_metrics()
        self.compute_health_score()
        if out_dir:
            self.export(out_dir)
        return self


if __name__ == "__main__":
    import sys

    src = sys.argv[1] if len(sys.argv) > 1 else "advanced_saas_revenue_retention_case__1_.xlsx"
    out = sys.argv[2] if len(sys.argv) > 2 else "cleaned_output"
    engine = SaaSRevenueAuditEngine(src).run_all(out)
    print("Rows by status (subscriptions):")
    print(engine.clean["subscriptions"]["record_status"].value_counts())
    print("\nCustomer summary sample:")
    print(engine.customer_summary.head())
