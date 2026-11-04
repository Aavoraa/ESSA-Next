"""The CRM's figures, for the Command Center's question box.

The CRM lives in the shop's database (Textile Retail Shop → CRM screens): the
customer master, customer orders, gift vouchers, coupons, loyalty points,
feedback, leads and walk-ins. This reads it the way services/pos_insights reads
the till — through pos_sales' read-only connection, every query degrading to
"nothing known" rather than raising, so a shop database from before the CRM
existed answers with zeros instead of an error.

WINDOWS. Most CRM timestamps are stored as naive UTC (`datetime.utcnow`), so a
period comes in as the UTC instants services/business_day makes. Two are not:
DATE columns (an order's date, a voucher's issue date) are compared with the
period's ISO days, and a walk-in's `created_at` is the shop's wall clock, so it is
compared with the local day.

WHERE. `loc` is None (everywhere) or a list of shop location ids — the same list
pos_insights narrows sales by. Records that carry a location (orders, voucher
sales, walk-ins, leads) honour it; the customer book, loyalty and feedback are the
company's and are answered company-wide.
"""
import re
from datetime import date, timedelta

from . import pos_sales

OPEN_ORDERS = ("new", "confirmed", "in_progress", "ready")
OPEN_LEADS = ("new", "contacted", "interested")
LIVE = " AND COALESCE(i.payment_status, 'paid') <> 'cancelled'"
#: Booleans are compared to a bound True/False rather than 0/1, which Postgres refuses.
_ACTIVE = "(inactive IS NULL OR inactive = ?)"


def available():
    return pos_sales.available()


def _t(table):
    return pos_sales.q(table)


def _rows(sql, params=()):
    return pos_sales._rows(sql, list(params))


def _one(sql, params=()):
    r = _rows(sql, params)
    return r[0] if r else None


def _n(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _ts(when):
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _loc(loc, col):
    """(sql, params) narrowing `col` to the location ids; '' when not narrowed."""
    if loc is None:
        return "", []
    if not loc:
        return " AND 1 = 0", []
    return f" AND {col} IN ({', '.join('?' for _ in loc)})", list(loc)


# ---------------------------------------------------------------------------
#  one customer
# ---------------------------------------------------------------------------
# A number however it is written: 9900011111, +91 99000 11111, 099000-11111,
# (990) 001-1111. Digits with spaces, dashes, dots or brackets between them.
_PHONE = re.compile(r"(?<![\d/])(\+?\(?\d[\d\s\-.()]{8,20}\d)(?![\d/])")
_CUST_CODE = re.compile(r"\bCUST0*(\d{1,9})\b", re.I)
#: The stored number with everything but its digits taken out, in SQL — so
#: "+91 99000 11111" on the customer and 9900011111 in the question are one number.
_DIGITS_SQL = ("REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(COALESCE({col},''),"
               "' ',''),'-',''),'+',''),'(',''),')',''),'.','')")


def phone_in(text):
    """The mobile number in a question, as digits — the last ten of an Indian
    number written with +91 or a leading 0 — or None."""
    for m in _PHONE.finditer(text or ""):
        raw = m.group(1)
        if re.search(r"\d[/\-.]\d{1,2}[/\-.]\d{2,4}\b", raw) and len(re.findall(r"\d+", raw)) == 3:
            continue                                   # 04-11-2026 is a date, not a number
        d = re.sub(r"\D", "", raw)
        if len(d) == 12 and d.startswith("91") or len(d) == 11 and d.startswith("0"):
            d = d[-10:]
        if len(d) == 10:                               # 13 digits is a barcode, not a phone
            return d
    return None


def _phone_match(col, digits):
    """(sql, params): `col` holds this number, whatever spaces or +91 it was saved with."""
    return f"{_DIGITS_SQL.format(col=col)} LIKE ?", [f"%{digits[-10:]}"]


def customers_by_digits(digits, limit=10):
    """Customers whose mobile contains these digits — a number half remembered,
    or its last few digits."""
    expr = _DIGITS_SQL.format(col="phone")
    alt = _DIGITS_SQL.format(col="alt_phone")
    return [{"id": r[0], "name": r[1], "phone": r[2], "points": _n(r[3]), "spent": _n(r[4])}
            for r in _rows(f"SELECT id, name, phone, loyalty_points, total_spent FROM {_t('customers')} "
                           f"WHERE {expr} LIKE ? OR {alt} LIKE ? ORDER BY total_spent DESC LIMIT {int(limit)}",
                           [f"%{digits}%", f"%{digits}%"])]


def find_customer(text):
    """The customer a question names — by mobile, loyalty card, customer code or
    name — as a dict, or None."""
    text = (text or "").strip()
    if not text:
        return None
    cols = ("c.id, c.name, c.phone, c.code, c.category, c.city, c.loyalty_points, "
            "c.total_spent, c.created_at, c.loyalty_card, c.customer_type, c.awaiting_approval")
    phone = phone_in(text)
    row = None
    if phone:
        a, pa = _phone_match("c.phone", phone)
        b, pb = _phone_match("c.alt_phone", phone)
        row = _one(f"SELECT {cols} FROM {_t('customers')} c WHERE {a} OR {b} ORDER BY c.id LIMIT 1", pa + pb)
    if row is None:
        m = _CUST_CODE.search(text)
        if m:
            row = _one(f"SELECT {cols} FROM {_t('customers')} c WHERE c.id = ?", [int(m.group(1))])
    if row is None:
        tok = text.strip("?.!,\"' ").upper()
        if tok and " " not in tok:
            row = _one(f"SELECT {cols} FROM {_t('customers')} c WHERE UPPER(c.code) = ? "
                       "OR UPPER(c.loyalty_card) = ? LIMIT 1", [tok, tok])
    if row is None and len(text) >= 3 and not phone:
        row = _one(f"SELECT {cols} FROM {_t('customers')} c WHERE LOWER(c.name) = ? LIMIT 1",
                   [text.lower()])
    if row is None:
        return None
    keys = ("id", "name", "phone", "code", "category", "city", "points", "spent", "created_at",
            "card", "type", "awaiting")
    c = dict(zip(keys, row))
    c["code"] = c["code"] or f"CUST{int(c['id']):06d}"
    bills = _one(f"SELECT COUNT(*), SUM(i.total), MAX(i.invoice_date) FROM {_t('invoices')} i "
                 f"WHERE i.customer_id = ?{LIVE}", [c["id"]]) or (0, 0, None)
    c["bills"], c["billed"], c["last_bill"] = int(bills[0] or 0), _n(bills[1]), bills[2]
    o = _one(f"SELECT COUNT(*) FROM {_t('customer_orders')} WHERE customer_id = ? "
             f"AND status IN ({', '.join('?' for _ in OPEN_ORDERS)})", [c["id"], *OPEN_ORDERS])
    c["open_orders"] = int((o or (0,))[0] or 0)
    recent = _rows(f"SELECT i.invoice_number, i.invoice_date, i.total FROM {_t('invoices')} i "
                   f"WHERE i.customer_id = ?{LIVE} ORDER BY i.invoice_date DESC LIMIT 10", [c["id"]])
    c["recent"] = [{"bill": r[0], "when": r[1], "total": _n(r[2])} for r in recent]
    return c


def find_customers_named(words, limit=10):
    """Customers whose name contains these words — for a question that is only a name."""
    words = [w for w in re.findall(r"[a-zA-Z]{3,}", words or "")][:4]
    if not words:
        return []
    where = " AND ".join("LOWER(name) LIKE ?" for _ in words)
    return [{"id": r[0], "name": r[1], "phone": r[2], "points": _n(r[3]), "spent": _n(r[4])}
            for r in _rows(f"SELECT id, name, phone, loyalty_points, total_spent FROM {_t('customers')} "
                           f"WHERE {where} ORDER BY total_spent DESC LIMIT {int(limit)}",
                           [f"%{w.lower()}%" for w in words])]


# ---------------------------------------------------------------------------
#  the customer book
# ---------------------------------------------------------------------------
def customers(start, end, loc=None):
    active = _one(f"SELECT COUNT(*) FROM {_t('customers')} WHERE {_ACTIVE}", [False]) or (0,)
    new = _one(f"SELECT COUNT(*) FROM {_t('customers')} WHERE created_at >= ? AND created_at < ?",
               [_ts(start), _ts(end)]) or (0,)
    pending = _one(f"SELECT COUNT(*) FROM {_t('customers')} WHERE awaiting_approval = ?", [True]) or (0,)
    lsql, lp = _loc(loc, "i.location_id")
    buyers = _rows(f"SELECT i.customer_id, COUNT(*) FROM {_t('invoices')} i "
                   f"WHERE i.customer_id IS NOT NULL AND i.invoice_date >= ? AND i.invoice_date < ?{LIVE}{lsql} "
                   "GROUP BY i.customer_id", [_ts(start), _ts(end), *lp])
    top = _rows(f"SELECT c.name, c.phone, COUNT(i.id), SUM(i.total) FROM {_t('invoices')} i "
                f"JOIN {_t('customers')} c ON c.id = i.customer_id "
                f"WHERE i.invoice_date >= ? AND i.invoice_date < ?{LIVE}{lsql} "
                "GROUP BY c.id, c.name, c.phone ORDER BY SUM(i.total) DESC LIMIT 20",
                [_ts(start), _ts(end), *lp])
    newest = _rows(f"SELECT name, phone, city, category, source, created_at FROM {_t('customers')} "
                   "WHERE created_at >= ? AND created_at < ? ORDER BY created_at DESC LIMIT 100",
                   [_ts(start), _ts(end)])
    return {"active": int(active[0] or 0), "new": int(new[0] or 0), "pending": int(pending[0] or 0),
            "buyers": len(buyers), "repeat": sum(1 for _, n in buyers if (n or 0) >= 2),
            "top": [{"Customer": r[0], "Mobile": r[1] or "—", "Bills": int(r[2] or 0), "Spent": _n(r[3])}
                    for r in top],
            "rows": [{"Customer": r[0], "Mobile": r[1] or "—", "City": r[2] or "—",
                      "Category": r[3] or "—", "Joined via": (r[4] or "counter").replace("_", " "),
                      "Joined": r[5]} for r in newest]}


def occasions(kind, d0, d1):
    """Customers whose birthday / anniversary falls between two days (any year)."""
    col = "dob" if kind == "birthday" else "anniversary"
    out = []
    for name, phone, when, spent in _rows(f"SELECT name, phone, {col}, total_spent FROM {_t('customers')} "
                                          f"WHERE {col} IS NOT NULL AND {_ACTIVE}", [False]):
        try:
            d = when if isinstance(when, date) else date.fromisoformat(str(when)[:10])
        except ValueError:
            continue
        for year in range(d0.year, d1.year + 1):
            try:
                on = d.replace(year=year)
            except ValueError:                 # 29 February in a short year
                on = date(year, 2, 28)
            if d0 <= on <= d1:
                out.append({"Customer": name, "Mobile": phone or "—", "On": on.strftime("%d-%m-%Y"),
                            "Lifetime spend": _n(spent), "_on": on})
                break
    out.sort(key=lambda r: r["_on"])
    for r in out:
        r.pop("_on")
    return out


# ---------------------------------------------------------------------------
#  customer orders
# ---------------------------------------------------------------------------
def _order_rows(where, params, limit=200):
    return _rows(
        f"SELECT o.number, o.order_date, o.delivery_date, c.name, COALESCE(o.contact_no, c.phone), "
        f"       o.status, "
        f"       (SELECT SUM(l.qty * l.rate) FROM {_t('customer_order_lines')} l WHERE l.order_id = o.id), "
        f"       (SELECT SUM(a.amount - COALESCE(a.refunded, 0)) FROM {_t('customer_advances')} a "
        f"         WHERE a.order_id = o.id) "
        f"FROM {_t('customer_orders')} o LEFT JOIN {_t('customers')} c ON c.id = o.customer_id "
        f"WHERE 1 = 1{where} ORDER BY o.delivery_date IS NULL, o.delivery_date, o.id LIMIT {int(limit)}",
        params)


def _order_dict(r):
    value, paid = _n(r[6]), _n(r[7])
    return {"Order": r[0], "Ordered": str(r[1] or "")[:10], "Deliver by": str(r[2] or "")[:10] or "—",
            "Customer": r[3] or "—", "Mobile": r[4] or "—", "Status": (r[5] or "").replace("_", " "),
            "Value": value, "Paid": paid, "Balance": round(max(0.0, value - paid), 2)}


def orders(d0, d1, loc=None):
    lsql, lp = _loc(loc, "o.location_id")
    open_in = f" AND o.status IN ({', '.join('?' for _ in OPEN_ORDERS)})"
    taken = [_order_dict(r) for r in _order_rows(f" AND o.order_date >= ? AND o.order_date <= ?{lsql}",
                                                 [d0.isoformat(), d1.isoformat(), *lp])]
    open_ = [_order_dict(r) for r in _order_rows(f"{open_in}{lsql}", [*OPEN_ORDERS, *lp], 500)]
    today = date.today().isoformat()
    return {"taken": taken, "taken_value": round(sum(r["Value"] for r in taken), 2),
            "open": open_, "open_value": round(sum(r["Value"] for r in open_), 2),
            "open_balance": round(sum(r["Balance"] for r in open_), 2),
            "due": [r for r in open_ if r["Deliver by"] != "—" and r["Deliver by"] <= today],
            "ready": [r for r in open_ if r["Status"] == "ready"]}


def find_order(number):
    rows = _order_rows(" AND UPPER(o.number) = ?", [number.upper()], 1)
    return _order_dict(rows[0]) if rows else None


# ---------------------------------------------------------------------------
#  gift vouchers and coupons
# ---------------------------------------------------------------------------
def _gv_spent_sql():
    return (f"(SELECT COALESCE(SUM(p.amount), 0) FROM {_t('invoice_payments')} p "
            f" JOIN {_t('invoices')} i ON i.id = p.invoice_id "
            f" WHERE p.method = 'gift_voucher' AND p.reference = v.code{LIVE})")


def gift_vouchers(d0, d1, start, end, loc=None):
    lsql, lp = _loc(loc, "b.location_id")
    sales = _rows(f"SELECT b.number, b.issued_on, c.name, c.phone, b.mode, b.amount, b.method, "
                  f"       (SELECT COUNT(*) FROM {_t('gift_vouchers')} v WHERE v.batch_id = b.id), "
                  f"       (SELECT SUM(v.value) FROM {_t('gift_vouchers')} v WHERE v.batch_id = b.id) "
                  f"FROM {_t('gift_voucher_batches')} b LEFT JOIN {_t('customers')} c ON c.id = b.customer_id "
                  f"WHERE b.issued_on >= ? AND b.issued_on <= ?{lsql} ORDER BY b.issued_on, b.id",
                  [d0.isoformat(), d1.isoformat(), *lp])
    rows = [{"Sale": r[0], "Issued": str(r[1] or "")[:10], "Bought by": r[2] or "—", "Mobile": r[3] or "—",
             "Mode": r[4] or "", "Vouchers": int(r[7] or 0), "Face value": _n(r[8]), "Received": _n(r[5]),
             "Paid by": (r[6] or "—").upper()} for r in sales]
    red = _one(f"SELECT COUNT(*), SUM(p.amount) FROM {_t('invoice_payments')} p "
               f"JOIN {_t('invoices')} i ON i.id = p.invoice_id "
               f"WHERE p.method = 'gift_voucher' AND i.invoice_date >= ? AND i.invoice_date < ?{LIVE}",
               [_ts(start), _ts(end)]) or (0, 0)
    out = _one(f"SELECT COUNT(*), SUM(v.value - {_gv_spent_sql()}) FROM {_t('gift_vouchers')} v "
               f"JOIN {_t('gift_voucher_batches')} b ON b.id = v.batch_id "
               f"WHERE (v.active IS NULL OR v.active = ?) AND (b.valid_to IS NULL OR b.valid_to >= ?) "
               f"AND v.value - {_gv_spent_sql()} > 0.009",
               [True, date.today().isoformat()]) or (0, 0)
    return {"rows": rows, "sold": sum(r["Vouchers"] for r in rows),
            "face": round(sum(r["Face value"] for r in rows), 2),
            "received": round(sum(r["Received"] for r in rows), 2),
            "redeemed_bills": int(red[0] or 0), "redeemed": round(_n(red[1]), 2),
            "live_vouchers": int(out[0] or 0), "outstanding": round(_n(out[1]), 2)}


def find_voucher(code):
    r = _one(f"SELECT v.code, v.value, {_gv_spent_sql()}, b.valid_to, v.active, c.name, v.given_to, b.number "
             f"FROM {_t('gift_vouchers')} v JOIN {_t('gift_voucher_batches')} b ON b.id = v.batch_id "
             f"LEFT JOIN {_t('customers')} c ON c.id = b.customer_id WHERE UPPER(v.code) = ?", [code.upper()])
    if not r:
        return None
    return {"code": r[0], "value": _n(r[1]), "spent": _n(r[2]), "left": round(max(0.0, _n(r[1]) - _n(r[2])), 2),
            "valid_to": str(r[3] or "")[:10], "active": r[4] not in (0, False), "buyer": r[5],
            "given_to": r[6], "sale": r[7]}


def coupons(start, end):
    issued = _one(f"SELECT COUNT(*) FROM {_t('coupons')} WHERE created_at >= ? AND created_at < ?",
                  [_ts(start), _ts(end)]) or (0,)
    by = _rows(f"SELECT cc.name, COUNT(r.id), SUM(r.amount) FROM {_t('coupon_redemptions')} r "
               f"JOIN {_t('coupons')} cp ON cp.id = r.coupon_id "
               f"JOIN {_t('coupon_campaigns')} cc ON cc.id = cp.campaign_id "
               f"WHERE r.voided_at IS NULL AND r.created_at >= ? AND r.created_at < ? "
               "GROUP BY cc.name ORDER BY SUM(r.amount) DESC", [_ts(start), _ts(end)])
    return {"issued": int(issued[0] or 0), "redeemed": sum(int(r[1] or 0) for r in by),
            "discount": round(sum(_n(r[2]) for r in by), 2),
            "rows": [{"Campaign": r[0], "Redeemed": int(r[1] or 0), "Discount given": _n(r[2])} for r in by]}


# ---------------------------------------------------------------------------
#  loyalty, feedback, leads, walk-ins
# ---------------------------------------------------------------------------
def loyalty(start, end):
    by = dict(_rows(f"SELECT CASE WHEN reason = 'earn' THEN 'earn' WHEN reason = 'redeem' THEN 'redeem' "
                    f"ELSE 'other' END, SUM(points) FROM {_t('loyalty_txns')} "
                    "WHERE created_at >= ? AND created_at < ? GROUP BY 1", [_ts(start), _ts(end)]))
    held = _one(f"SELECT SUM(loyalty_points), COUNT(*) FROM {_t('customers')} WHERE loyalty_points > 0") or (0, 0)
    top = _rows(f"SELECT name, phone, loyalty_points, total_spent FROM {_t('customers')} "
                "WHERE loyalty_points > 0 ORDER BY loyalty_points DESC LIMIT 20")
    return {"earned": round(_n(by.get("earn")), 2), "redeemed": round(-_n(by.get("redeem")), 2),
            "other": round(_n(by.get("other")), 2), "held": round(_n(held[0]), 2),
            "holders": int(held[1] or 0),
            "rows": [{"Customer": r[0], "Mobile": r[1] or "—", "Points": _n(r[2]), "Lifetime spend": _n(r[3])}
                     for r in top]}


def feedback(start, end, alert_at=2):
    n, avg = _one(f"SELECT COUNT(*), AVG(rating) FROM {_t('customer_feedback')} "
                  "WHERE created_at >= ? AND created_at < ?", [_ts(start), _ts(end)]) or (0, None)
    dist = dict(_rows(f"SELECT rating, COUNT(*) FROM {_t('customer_feedback')} "
                      "WHERE created_at >= ? AND created_at < ? GROUP BY rating", [_ts(start), _ts(end)]))
    low = _rows(f"SELECT f.created_at, c.name, c.phone, f.rating, f.comments FROM {_t('customer_feedback')} f "
                f"LEFT JOIN {_t('customers')} c ON c.id = f.customer_id "
                "WHERE f.created_at >= ? AND f.created_at < ? AND f.rating <= ? ORDER BY f.created_at DESC LIMIT 50",
                [_ts(start), _ts(end), alert_at])
    questions = _rows(f"SELECT q.text, COUNT(a.id), AVG(a.score), q.kind FROM {_t('feedback_answers')} a "
                      f"JOIN {_t('feedback_questions')} q ON q.id = a.question_id "
                      f"JOIN {_t('customer_feedback')} f ON f.id = a.feedback_id "
                      "WHERE f.created_at >= ? AND f.created_at < ? GROUP BY q.id, q.text, q.kind",
                      [_ts(start), _ts(end)])
    return {"count": int(n or 0), "average": round(_n(avg), 2) if avg is not None else None,
            "happy": sum(int(dist.get(r, 0) or 0) for r in (4, 5)),
            "unhappy": sum(int(dist.get(r, 0) or 0) for r in range(1, alert_at + 1)),
            "low": [{"When": r[0], "Customer": r[1] or "walk-in", "Mobile": r[2] or "—",
                     "Rating": int(r[3] or 0), "Comment": r[4] or ""} for r in low],
            "questions": [{"Question": r[0], "Answers": int(r[1] or 0),
                           "Score": (f"{_n(r[2]):.2f} ★" if r[3] == "rating" else f"{_n(r[2]) * 100:.0f}% yes")
                           if r[2] is not None else "—"} for r in questions]}


def leads(start, end, loc=None):
    lsql, lp = _loc(loc, "l.location_id")
    open_in = f"l.status IN ({', '.join('?' for _ in OPEN_LEADS)})"
    today = date.today().isoformat()
    open_ = _rows(f"SELECT l.name, l.phone, l.source, l.interest, l.expected_value, l.status, "
                  f"       l.next_follow_up, COALESCE(u.full_name, u.username) "
                  f"FROM {_t('leads')} l LEFT JOIN {_t('users')} u ON u.id = l.assigned_to_id "
                  f"WHERE {open_in}{lsql} ORDER BY l.next_follow_up IS NULL, l.next_follow_up LIMIT 300",
                  [*OPEN_LEADS, *lp])
    rows = [{"Lead": r[0], "Mobile": r[1] or "—", "Source": (r[2] or "").replace("_", " "),
             "Interested in": r[3] or "—", "Expected": _n(r[4]), "Status": r[5],
             "Follow up": str(r[6] or "")[:10] or "—", "Assigned to": r[7] or "—"} for r in open_]
    new = _one(f"SELECT COUNT(*) FROM {_t('leads')} l WHERE l.created_at >= ? AND l.created_at < ?{lsql}",
               [_ts(start), _ts(end), *lp]) or (0,)
    closed = dict(_rows(f"SELECT l.status, COUNT(*) FROM {_t('leads')} l WHERE l.status IN ('won', 'lost') "
                        f"AND l.updated_at >= ? AND l.updated_at < ?{lsql} GROUP BY l.status",
                        [_ts(start), _ts(end), *lp]))
    return {"open": len(rows), "rows": rows, "new": int(new[0] or 0),
            "due": sum(1 for r in rows if r["Follow up"] == today),
            "overdue": sum(1 for r in rows if r["Follow up"] != "—" and r["Follow up"] < today),
            "pipeline": round(sum(r["Expected"] for r in rows), 2),
            "won": int(closed.get("won", 0) or 0), "lost": int(closed.get("lost", 0) or 0)}


def walkins(d0, d1, loc=None):
    """Customer Divert — walk-ins between two LOCAL days (that table keeps wall-clock time)."""
    lsql, lp = _loc(loc, "v.location_id")
    rows = _rows(f"SELECT v.created_at, COALESCE(c.name, v.name), v.phone, v.looking_for, f.name, "
                 f"       COALESCE(u.full_name, u.username), v.status, i.invoice_number, i.total "
                 f"FROM {_t('customer_visits')} v "
                 f"LEFT JOIN {_t('customers')} c ON c.id = v.customer_id "
                 f"LEFT JOIN {_t('floors')} f ON f.id = v.floor_id "
                 f"LEFT JOIN {_t('users')} u ON u.id = v.staff_id "
                 f"LEFT JOIN {_t('invoices')} i ON i.id = v.invoice_id "
                 f"WHERE v.created_at >= ? AND v.created_at < ?{lsql} ORDER BY v.created_at",
                 [d0.isoformat(), (d1 + timedelta(days=1)).isoformat(), *lp])
    out = [{"In at": str(r[0] or "")[:16], "Customer": r[1] or "—", "Mobile": r[2] or "—",
            "Looking for": r[3] or "—", "Floor": r[4] or "—", "Salesperson": r[5] or "—",
            "Status": r[6] or "", "Bill": r[7] or "—", "Bought": _n(r[8])} for r in rows]
    billed = [r for r in out if r["Status"] == "billed"]
    return {"rows": out, "total": len(out), "billed": len(billed),
            "left": sum(1 for r in out if r["Status"] == "left"),
            "open": sum(1 for r in out if r["Status"] in ("waiting", "attending")),
            "bought": round(sum(r["Bought"] for r in billed), 2)}
