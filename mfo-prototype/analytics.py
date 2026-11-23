# analytics.py
# ------------------------------------------------------------
# Portfolio analytics for Microfinance IS (read-only: it never changes data).
#   portfolio_report()  -> dict with the key numbers of the loan portfolio
#   python analytics.py -> prints the report in the console
#
# Definitions (used in the test plan and in the report):
#   approval_rate   = Approved / (Approved + Rejected). Applications that are still
#                     "Pending Admin Review" are NOT counted: no decision yet.
#   overdue payment = status 'Pending' AND due_date < today.
#   overdue_rate    = overdue_amount / (all Pending amount_due), i.e. the share of
#                     the unpaid debt that is already late (simple "portfolio at risk").
# The repayments table has only 'Pending' / 'Paid', so "overdue" is calculated here
# from the dates; it is not stored in the database.
# ------------------------------------------------------------
from datetime import date

import database


def _ratio(part, whole):
    # Safe division: an empty portfolio gives 0.0 instead of ZeroDivisionError.
    return round(part / whole, 4) if whole else 0.0


def portfolio_report(today=None):
    if today is None:
        today = date.today()
    today_text = today.isoformat()  # ISO dates compare correctly as text

    conn = database.get_connection()
    try:
        # ---- applications ----
        by_status = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM loan_applications GROUP BY status")}
        by_type = {row["approval_type"]: row["n"] for row in conn.execute(
            "SELECT approval_type, COUNT(*) AS n FROM loan_applications GROUP BY approval_type")}
        approved = by_status.get("Approved", 0)
        rejected = by_status.get("Rejected", 0)
        pending = by_status.get("Pending Admin Review", 0)

        # ---- loans ----
        loans = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(approved_amount), 0) AS total FROM loans").fetchone()

        # ---- repayments ----
        money = conn.execute(
            """SELECT
                 COALESCE(SUM(amount_due), 0)                                  AS scheduled,
                 COALESCE(SUM(CASE WHEN status = 'Paid'    THEN amount_due END), 0) AS paid,
                 COALESCE(SUM(CASE WHEN status = 'Pending' THEN amount_due END), 0) AS pending
               FROM repayments""").fetchone()
        overdue = conn.execute(
            """SELECT COUNT(*) AS n,
                      COALESCE(SUM(amount_due), 0) AS total,
                      COUNT(DISTINCT loan_id) AS loans
               FROM repayments WHERE status = 'Pending' AND due_date < ?""",
            (today_text,)).fetchone()

        return {
            "as_of": today_text,
            "applications": {
                "total": approved + rejected + pending + sum(
                    n for s, n in by_status.items()
                    if s not in ("Approved", "Rejected", "Pending Admin Review")),
                "approved": approved,
                "rejected": rejected,
                "pending_review": pending,
                "auto": by_type.get("Auto", 0),
                "manual": by_type.get("Manual", 0),
                "approval_rate": _ratio(approved, approved + rejected),
            },
            "loans": {
                "count": loans["n"],
                "total_issued": round(loans["total"], 2),
                "average_amount": round(loans["total"] / loans["n"], 2) if loans["n"] else 0.0,
            },
            "repayments": {
                "scheduled_amount": round(money["scheduled"], 2),
                "collected_amount": round(money["paid"], 2),
                "outstanding_amount": round(money["pending"], 2),
                "overdue_count": overdue["n"],
                "overdue_amount": round(overdue["total"], 2),
                "overdue_loans": overdue["loans"],
                "overdue_rate": _ratio(overdue["total"], money["pending"]),
            },
        }
    finally:
        conn.close()


def print_report(report):
    a, l, r = report["applications"], report["loans"], report["repayments"]
    print(f"Portfolio report as of {report['as_of']}")
    print(f"  Applications: {a['total']} (approved {a['approved']}, rejected {a['rejected']}, "
          f"pending review {a['pending_review']}; auto {a['auto']}, manual {a['manual']})")
    print(f"  Approval rate: {a['approval_rate'] * 100:.1f}%")
    print(f"  Loans: {l['count']}, issued {l['total_issued']:,.2f} KZT, average {l['average_amount']:,.2f} KZT")
    print(f"  Collected: {r['collected_amount']:,.2f}  Outstanding: {r['outstanding_amount']:,.2f} KZT")
    print(f"  Overdue: {r['overdue_count']} payments on {r['overdue_loans']} loans, "
          f"{r['overdue_amount']:,.2f} KZT ({r['overdue_rate'] * 100:.1f}% of outstanding)")


if __name__ == "__main__":
    print_report(portfolio_report())
