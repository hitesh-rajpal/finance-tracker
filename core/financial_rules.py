"""Expected-vs-actual cross-checking: define what you expect from a bank,
institute, your office, or a known person (savings interest at a rate,
credit-card cashback at a rate, a loan on its amortization schedule, or any
other fixed recurring transfer), then check whether the real transactions
match, are missing, or differ.
"""
import hashlib
import pandas as pd
from dateutil.relativedelta import relativedelta

FREQUENCIES = ["Monthly", "Quarterly", "Yearly"]
RULE_TYPES = ["Interest", "Cashback", "Loan EMI", "Recurring"]
COUNTERPARTY_TYPES = ["Bank", "Institute", "Office", "Relative", "Other"]


def make_rule_id(name: str, rule_type: str) -> str:
    return hashlib.sha256(f"{name}|{rule_type}".encode("utf-8")).hexdigest()[:16]


def amortization_schedule(principal: float, annual_rate: float, tenure_months: int, start_date) -> pd.DataFrame:
    """Standard reducing-balance EMI schedule. start_date is the first EMI's date."""
    start_date = pd.to_datetime(start_date)
    r = (annual_rate / 12) / 100
    if r == 0:
        emi = principal / tenure_months
    else:
        emi = principal * r * (1 + r) ** tenure_months / ((1 + r) ** tenure_months - 1)
    rows = []
    balance = principal
    for m in range(tenure_months):
        interest = balance * r
        principal_component = emi - interest
        balance = max(0.0, balance - principal_component)
        rows.append({
            "month": m + 1,
            "date": (start_date + relativedelta(months=m)).date(),
            "emi": round(emi, 2),
            "principal": round(principal_component, 2),
            "interest": round(interest, 2),
            "balance": round(balance, 2),
        })
    return pd.DataFrame(rows)


def period_bounds(frequency: str, target_date):
    """(period_start, period_end) containing target_date, sized per frequency."""
    target_date = pd.to_datetime(target_date)
    if frequency == "Yearly":
        start = target_date.replace(month=1, day=1)
        end = start + relativedelta(years=1) - pd.Timedelta(days=1)
    elif frequency == "Quarterly":
        q_start_month = 3 * ((target_date.month - 1) // 3) + 1
        start = target_date.replace(month=q_start_month, day=1)
        end = start + relativedelta(months=3) - pd.Timedelta(days=1)
    else:  # Monthly
        start = target_date.replace(day=1)
        end = start + relativedelta(months=1) - pd.Timedelta(days=1)
    return start, end


def expected_amount(rule: dict, period_start, period_end, actual_df: pd.DataFrame) -> float | None:
    rule_type = rule["rule_type"]

    if rule_type == "Interest":
        rate = rule.get("rate_percent") or 0
        basis = rule.get("basis_amount") or 0
        fraction = {"Monthly": 1 / 12, "Quarterly": 1 / 4, "Yearly": 1.0}.get(rule.get("frequency"), 1 / 12)
        return round(basis * (rate / 100) * fraction, 2)

    if rule_type == "Cashback":
        rate = rule.get("rate_percent") or 0
        base = 0.0
        if not actual_df.empty:
            spend = actual_df[
                (actual_df["account_key"] == rule.get("account_key"))
                & (actual_df["direction"] == "debit")
                & (actual_df["date"] >= pd.to_datetime(period_start))
                & (actual_df["date"] <= pd.to_datetime(period_end))
            ]
            if rule.get("basis_category"):
                spend = spend[spend["category"] == rule["basis_category"]]
            base = spend["amount"].sum()
        return round(base * (rate / 100), 2)

    if rule_type == "Loan EMI":
        if not (rule.get("principal") and rule.get("tenure_months") and rule.get("start_date")):
            return None
        schedule = amortization_schedule(
            rule["principal"], rule.get("annual_rate") or 0, int(rule["tenure_months"]), rule["start_date"]
        )
        sched_dates = pd.to_datetime(schedule["date"])
        window = schedule[(sched_dates >= pd.to_datetime(period_start)) & (sched_dates <= pd.to_datetime(period_end))]
        return round(window["emi"].sum(), 2) if not window.empty else None

    if rule_type == "Recurring":
        return rule.get("expected_amount")

    return None


def _expected_direction(rule: dict) -> str:
    if rule["rule_type"] == "Loan EMI":
        return "debit"
    if rule["rule_type"] in ("Interest", "Cashback"):
        return "credit"
    return rule.get("direction") or "credit"


def cross_check(rule: dict, actual_df: pd.DataFrame, period_start, period_end, tolerance_pct: float = 0.05) -> dict:
    """Returns expected/actual amounts and a Matched/Mismatch/Missing/Unknown status."""
    expected = expected_amount(rule, period_start, period_end, actual_df)
    direction = _expected_direction(rule)

    matches = pd.DataFrame()
    if not actual_df.empty:
        matches = actual_df[
            (actual_df["account_key"] == rule.get("account_key"))
            & (actual_df["direction"] == direction)
            & (actual_df["date"] >= pd.to_datetime(period_start))
            & (actual_df["date"] <= pd.to_datetime(period_end))
        ]
        if rule.get("match_category"):
            matches = matches[matches["category"] == rule["match_category"]]

    actual_total = float(matches["amount"].sum()) if not matches.empty else 0.0

    if expected is None:
        status = "Unknown (missing rule inputs)" if actual_total == 0 else "Found (no expectation set)"
    elif expected == 0:
        status = "Matched" if actual_total == 0 else "Mismatch"
    elif actual_total == 0:
        status = "Missing"
    elif abs(actual_total - expected) <= max(1.0, expected * tolerance_pct):
        status = "Matched"
    else:
        status = "Mismatch"

    return {
        "expected": expected,
        "actual": actual_total,
        "actual_count": int(len(matches)),
        "status": status,
    }
