-- ============================================================================
-- SaaS Revenue & Retention Audit — SQL Layer
-- Tables used (built by sql/build_db.py from the Python cleaning engine):
--   raw_subscriptions      : subscriptions exactly as Finance/source system has them
--   clean_subscriptions    : subscriptions + record_status (VALID/SUSPICIOUS/INVALID)
--   clean_invoices         : invoices + record_status
--   clean_product_usage    : usage + record_status
--   clean_support_cases    : support cases + record_status
--   customer_summary       : one row per customer with trusted metrics + health score
-- ============================================================================


-- ============================================================================
-- Q1. Trusted MRR vs Reported MRR by plan & region, with Revenue Overstatement %
-- ============================================================================
WITH reported AS (
    SELECT region, plan_name, SUM(monthly_fee) AS reported_mrr
    FROM raw_subscriptions
    WHERE subscription_status = 'Active'
    GROUP BY region, plan_name
),
trusted AS (
    SELECT region, plan_name,
           SUM(monthly_fee * (1 - discount_pct / 100.0)) AS trusted_mrr
    FROM clean_subscriptions
    WHERE subscription_status_norm = 'Active' AND record_status != 'INVALID'
    GROUP BY region, plan_name
)
SELECT
    COALESCE(r.region, t.region)       AS region,
    COALESCE(r.plan_name, t.plan_name) AS plan_name,
    ROUND(COALESCE(r.reported_mrr, 0), 2) AS reported_mrr,
    ROUND(COALESCE(t.trusted_mrr, 0), 2)  AS trusted_mrr,
    ROUND(
        CASE WHEN COALESCE(r.reported_mrr, 0) = 0 THEN 0
             ELSE (COALESCE(r.reported_mrr, 0) - COALESCE(t.trusted_mrr, 0))
                  / r.reported_mrr * 100
        END, 2) AS revenue_overstatement_pct
FROM reported r
FULL OUTER JOIN trusted t
    ON r.region = t.region AND r.plan_name = t.plan_name
ORDER BY revenue_overstatement_pct DESC;


-- ============================================================================
-- Q2. High revenue + low usage + above-average support escalations
--     (uses NTILE window functions for the percentile cuts)
-- ============================================================================
WITH escalations AS (
    SELECT customer_id, COUNT(*) AS escalation_count
    FROM clean_support_cases
    WHERE record_status != 'INVALID' AND case_status_norm = 'Escalated'
    GROUP BY customer_id
),
base AS (
    SELECT
        cs.customer_id,
        cs.effective_monthly_revenue,
        cs.plan_name,
        cs.usage_intensity_score,
        cs.average_csat,
        cs.customer_health_score,
        COALESCE(e.escalation_count, 0) AS escalation_count,
        NTILE(5)  OVER (ORDER BY cs.effective_monthly_revenue) AS rev_quintile,   -- 5 = top 20%
        NTILE(10) OVER (ORDER BY cs.usage_intensity_score)     AS usage_decile   -- 1-3 = bottom 30%
    FROM customer_summary cs
    LEFT JOIN escalations e ON e.customer_id = cs.customer_id
),
company_avg AS (
    SELECT AVG(COALESCE(e.escalation_count, 0)) AS avg_escalations
    FROM customer_summary cs
    LEFT JOIN escalations e ON e.customer_id = cs.customer_id
)
SELECT
    b.customer_id,
    ROUND(b.effective_monthly_revenue, 2) AS revenue,
    b.plan_name,
    b.usage_intensity_score AS usage_score,
    b.escalation_count,
    ROUND(b.average_csat, 2) AS csat,
    b.customer_health_score AS health_score
FROM base b, company_avg
WHERE b.rev_quintile = 5
  AND b.usage_decile <= 3
  AND b.escalation_count > company_avg.avg_escalations
ORDER BY b.effective_monthly_revenue DESC;


-- ============================================================================
-- Q3. Plans where high discounting is NOT producing better retention
-- ============================================================================
WITH plan_stats AS (
    SELECT
        plan_name,
        AVG(discount_pct) AS avg_discount_pct,
        SUM(CASE WHEN subscription_status_norm = 'Active'    THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS renewal_rate,
        SUM(CASE WHEN subscription_status_norm = 'Cancelled' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS cancellation_rate,
        SUM(CASE WHEN subscription_status_norm = 'Active'
                 THEN monthly_fee * (1 - discount_pct / 100.0) ELSE 0 END) AS trusted_mrr
    FROM clean_subscriptions
    WHERE record_status != 'INVALID' AND plan_name IS NOT NULL
    GROUP BY plan_name
),
plan_health AS (
    SELECT plan_name, AVG(customer_health_score) AS avg_health_score
    FROM customer_summary
    WHERE plan_name IS NOT NULL
    GROUP BY plan_name
),
company AS (
    SELECT AVG(avg_discount_pct) AS co_discount, AVG(renewal_rate) AS co_renewal
    FROM plan_stats
)
SELECT
    p.plan_name,
    ROUND(p.avg_discount_pct, 2)        AS avg_discount_pct,
    ROUND(p.renewal_rate * 100, 2)      AS renewal_rate_pct,
    ROUND(p.cancellation_rate * 100, 2) AS cancellation_rate_pct,
    ROUND(h.avg_health_score, 2)        AS avg_health_score,
    ROUND(p.trusted_mrr, 2)             AS trusted_mrr
FROM plan_stats p
JOIN plan_health h ON h.plan_name = p.plan_name
CROSS JOIN company c
WHERE p.avg_discount_pct > c.co_discount
  AND p.renewal_rate < c.co_renewal
ORDER BY p.avg_discount_pct DESC;


-- ============================================================================
-- Q4. Revenue at risk (active + High Risk health + positive trusted MRR)
--     — three breakdowns as requested
-- ============================================================================
-- 4a. By region
SELECT region, COUNT(*) AS at_risk_customers, ROUND(SUM(MRR), 2) AS revenue_at_risk
FROM customer_summary
WHERE subscription_status_norm = 'Active' AND risk_category = 'High Risk' AND MRR > 0
GROUP BY region
ORDER BY revenue_at_risk DESC;

-- 4b. By plan
SELECT plan_name, COUNT(*) AS at_risk_customers, ROUND(SUM(MRR), 2) AS revenue_at_risk
FROM customer_summary
WHERE subscription_status_norm = 'Active' AND risk_category = 'High Risk' AND MRR > 0
GROUP BY plan_name
ORDER BY revenue_at_risk DESC;

-- 4c. By sales channel
SELECT sales_channel, COUNT(*) AS at_risk_customers, ROUND(SUM(MRR), 2) AS revenue_at_risk
FROM customer_summary
WHERE subscription_status_norm = 'Active' AND risk_category = 'High Risk' AND MRR > 0
GROUP BY sales_channel
ORDER BY revenue_at_risk DESC;


-- ============================================================================
-- Q5. Operational contradictions — top 20 by financial impact
-- ============================================================================
WITH usage_thresh AS (
    SELECT AVG(usage_intensity_score) AS avg_usage FROM customer_summary
),
contradictions AS (
    -- Active subscription with repeated failed payments
    SELECT customer_id,
           'Active subscription with repeated failed payments' AS contradiction_type,
           'Payment failure rate ' || ROUND(payment_failure_rate * 100, 1) || '% while subscription is Active' AS description,
           MRR AS financial_impact
    FROM customer_summary
    WHERE subscription_status_norm = 'Active' AND payment_failure_rate > 0.5

    UNION ALL
    -- High product usage but cancellation
    SELECT customer_id,
           'High product usage but cancelled',
           'Usage intensity ' || usage_intensity_score || ' (above company average) yet subscription Cancelled',
           effective_monthly_revenue
    FROM customer_summary, usage_thresh
    WHERE subscription_status_norm = 'Cancelled' AND usage_intensity_score > usage_thresh.avg_usage

    UNION ALL
    -- Low usage on Enterprise plan
    SELECT customer_id,
           'Low usage on Enterprise plan',
           'Usage intensity only ' || usage_intensity_score || ' on an Enterprise contract',
           effective_monthly_revenue
    FROM customer_summary
    WHERE plan_name = 'Enterprise' AND usage_intensity_score < 25

    UNION ALL
    -- High CSAT despite repeated escalations
    SELECT customer_id,
           'High CSAT despite repeated escalations',
           'Average CSAT ' || ROUND(average_csat, 1) || ' but escalation rate ' || ROUND(support_escalation_rate * 100, 1) || '%',
           effective_monthly_revenue
    FROM customer_summary
    WHERE average_csat >= 4 AND support_escalation_rate > 0.3

    UNION ALL
    -- Paid invoice with an invalid payment timeline
    SELECT s.customer_id,
           'Paid invoice with invalid payment timeline',
           'Invoice ' || i.invoice_id || ': ' || i.status_reason,
           i.invoice_amount
    FROM clean_invoices i
    JOIN clean_subscriptions s ON s.subscription_id = i.subscription_id
    WHERE i.payment_status_norm = 'Paid' AND i.record_status = 'INVALID'

    UNION ALL
    -- High revenue customer with very poor health score
    SELECT customer_id,
           'High revenue customer with very poor health score',
           'Revenue ' || ROUND(effective_monthly_revenue, 0) || ' with health score ' || customer_health_score,
           effective_monthly_revenue
    FROM customer_summary
    WHERE customer_health_score < 30
      AND effective_monthly_revenue > (SELECT AVG(effective_monthly_revenue) * 1.5 FROM customer_summary)
)
SELECT *
FROM contradictions
WHERE financial_impact IS NOT NULL
ORDER BY financial_impact DESC
LIMIT 20;


-- ============================================================================
-- Q6. Final executive table
-- ============================================================================
DROP TABLE IF EXISTS executive_customer_risk_summary;

CREATE TABLE executive_customer_risk_summary AS
WITH invoice_agg AS (
    SELECT s.customer_id,
           SUM(i.invoice_amount) AS total_invoice_value
    FROM clean_invoices i
    JOIN clean_subscriptions s ON s.subscription_id = i.subscription_id
    WHERE i.record_status != 'INVALID'
    GROUP BY s.customer_id
),
case_agg AS (
    SELECT customer_id,
           COUNT(*) AS support_case_count,
           SUM(CASE WHEN case_status_norm = 'Escalated' THEN 1 ELSE 0 END) AS escalation_count
    FROM clean_support_cases
    WHERE record_status != 'INVALID'
    GROUP BY customer_id
)
SELECT
    cs.customer_id,
    cs.region,
    cs.plan_name,
    cs.sales_channel,
    ROUND(rs.reported_mrr, 2)                       AS reported_mrr,
    ROUND(cs.MRR, 2)                                AS trusted_mrr,
    ROUND(COALESCE(ia.total_invoice_value, 0), 2)   AS total_invoice_value,
    ROUND(cs.payment_failure_rate, 4)               AS payment_failure_rate,
    cs.usage_intensity_score,
    COALESCE(ca.support_case_count, 0)              AS support_case_count,
    COALESCE(ca.escalation_count, 0)                AS escalation_count,
    ROUND(cs.average_csat, 2)                       AS average_csat,
    cs.customer_health_score,
    cs.risk_category,
    ROUND(
        CASE WHEN cs.subscription_status_norm = 'Active'
                  AND cs.risk_category = 'High Risk'
                  AND cs.MRR > 0
             THEN cs.MRR ELSE 0 END, 2)              AS revenue_at_risk,
    CASE
        WHEN cs.risk_category = 'High Risk' AND cs.MRR > 0 AND cs.subscription_status_norm = 'Active'
             THEN 'Immediate Action'
        WHEN cs.risk_category = 'High Risk'
             THEN 'Investigate'
        WHEN cs.risk_category = 'Watchlist' AND cs.MRR > (SELECT AVG(MRR) FROM customer_summary)
             THEN 'Monitor Closely'
        WHEN cs.risk_category = 'Watchlist'
             THEN 'Monitor'
        ELSE 'Low Priority'
    END AS recommended_priority
FROM customer_summary cs
LEFT JOIN (
    SELECT region, plan_name, SUM(monthly_fee) AS reported_mrr
    FROM raw_subscriptions
    WHERE subscription_status = 'Active'
    GROUP BY region, plan_name
) rs ON rs.region = cs.region AND rs.plan_name = cs.plan_name
LEFT JOIN invoice_agg ia ON ia.customer_id = cs.customer_id
LEFT JOIN case_agg ca ON ca.customer_id = cs.customer_id;

SELECT * FROM executive_customer_risk_summary ORDER BY revenue_at_risk DESC LIMIT 20;
