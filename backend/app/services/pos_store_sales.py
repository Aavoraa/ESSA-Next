"""What each SHOP sold — read from the till's own database, grouped by branch.

services/pos_sales.py answers "what has this warehouse ITEM sold", which is what
dead stock and the item locator ask. This answers the other question: "what did
this BRANCH take", which is what a consolidated dashboard asks. Same wall, same
read-only rule, same transport (pos_sales owns it) — a different GROUP BY.

THE JOIN BACK TO A WAREHOUSE IS BY NAME, and that is not a shortcut. The shop
keeps its own `locations` table and syncs it from ours by name, case-insensitively
(see the shop's app/places.sync_locations and services/locations.mirror_to_options);
`Store.name` is unique company-wide precisely so it can be the join key. There is
no id shared across the two databases to use instead.

So a branch whose name does not match any store of ours is NOT silently dropped
and NOT silently attributed: it comes back under `unmatched`, with its money, and
the caller shows it. A figure that vanishes because two spellings drifted apart is
the failure this arrangement is most prone to, and the only defence is to say so.

Bills with no branch at all — raised before the till had counters — are reported
separately again, as `unplaced`. They are real money and they belong to no store.
"""
import datetime as dt

from . import pos_sales

#: Bills in these states are money taken. The shop marks a bill `paid` or
#: `pending`; both were rung up, and a dashboard that counted only settled bills
#: would disagree with the till's own day-end figure.
_COUNTED = ("paid", "pending")


def _norm(name):
    return " ".join(str(name or "").split()).lower()


def _window(days=None, date_from=None, date_to=None, today=None):
    """(from, to) as ISO dates. A day window is inclusive at both ends."""
    end = date_to or (today or dt.date.today()).isoformat()
    if date_from:
        return date_from, end
    n = max(1, min(int(days or 14), 400))
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=n - 1)).isoformat()
    return start, end


def by_store(db, days=None, date_from=None, date_to=None, warehouse_id=None,
             today=None):
    """Sales per branch over a window, mapped onto the warehouse that supplies it.

    Returns `available: False` with a readable reason rather than an empty table
    when the shop cannot be read at all — nothing sold and cannot ask are
    different answers, and a dashboard must not present the second as the first.
    """
    start, end = _window(days, date_from, date_to, today)
    if not pos_sales.available():
        return {"available": False,
                "reason": "The shop (POS) module is not installed here",
                "source": None, "window": {"from": start, "to": end},
                "totals": {}, "stores": [], "unmatched": [], "unplaced": {}}

    where, params = pos_sales._window(start, end, "i.invoice_date")
    states = ",".join("?" for _ in _COUNTED)
    sold = pos_sales._rows(
        "SELECT i.location_id, MAX(l.name), COUNT(DISTINCT i.id), "
        "       SUM(i.total), SUM(i.subtotal), SUM(COALESCE(i.discount,0)), "
        "       SUM(COALESCE(i.cgst,0)+COALESCE(i.sgst,0)+COALESCE(i.igst,0)), "
        "       MAX(i.invoice_date) "
        "FROM " + pos_sales.q("invoices") + " i "
        "LEFT JOIN " + pos_sales.q("locations") + " l ON l.id = i.location_id "
        f"WHERE i.payment_status IN ({states})" + where +
        " GROUP BY i.location_id",
        list(_COUNTED) + params)

    # Units sold, which the money columns cannot give: a bill's total says
    # nothing about how many garments left the shelf.
    units = dict((r[0], float(r[1] or 0)) for r in pos_sales._rows(
        "SELECT i.location_id, SUM(ii.quantity) "
        "FROM " + pos_sales.q("invoice_items") + " ii "
        "JOIN " + pos_sales.q("invoices") + " i ON i.id = ii.invoice_id "
        f"WHERE i.payment_status IN ({states})" + where +
        " GROUP BY i.location_id",
        list(_COUNTED) + params))

    # Returns come off, counted in the window they HAPPENED in — the same rule
    # pos_sales.sales_by_product follows, and for the same reason.
    ret_where, ret_params = pos_sales._window(start, end, "cn.created_at")
    returns = {}
    for lid, amount, qty in pos_sales._rows(
            "SELECT i.location_id, SUM(COALESCE(cn.total,0)), SUM(ci.quantity) "
            "FROM " + pos_sales.q("credit_note_items") + " ci "
            "JOIN " + pos_sales.q("credit_notes") + " cn ON cn.id = ci.credit_note_id "
            "LEFT JOIN " + pos_sales.q("invoices") + " i ON i.id = cn.invoice_id "
            "WHERE 1=1" + ret_where + " GROUP BY i.location_id", ret_params):
        returns[lid] = {"amount": float(amount or 0), "qty": float(qty or 0)}

    # our stores, by the name the shop knows them by
    from .. import models
    ours = {}
    for s in db.query(models.Store).all():
        ours[_norm(s.name)] = s

    stores, unmatched = [], []
    unplaced = {"bills": 0, "gross": 0.0, "net": 0.0, "units": 0.0}
    for (lid, lname, bills, total, subtotal, discount, tax, last) in sold:
        ret = returns.get(lid, {"amount": 0.0, "qty": 0.0})
        row = {
            "location_id": lid, "store": lname or None,
            "bills": int(bills or 0),
            "units": round(units.get(lid, 0.0) - ret["qty"], 3),
            "gross": round(float(total or 0), 2),
            "taxable": round(float(subtotal or 0) - float(discount or 0), 2),
            "discount": round(float(discount or 0), 2),
            "tax": round(float(tax or 0), 2),
            "returns": round(ret["amount"], 2),
            "net": round(float(total or 0) - ret["amount"], 2),
            "last_sale": (str(last or "")[:10]) or None,
        }
        if lid is None or not lname:
            unplaced["bills"] += row["bills"]
            unplaced["gross"] = round(unplaced["gross"] + row["gross"], 2)
            unplaced["net"] = round(unplaced["net"] + row["net"], 2)
            unplaced["units"] = round(unplaced["units"] + row["units"], 3)
            continue
        store = ours.get(_norm(lname))
        if store is None:
            unmatched.append(row)
            continue
        wh = store.warehouse
        row.update({"store_id": store.id, "store_code": store.code,
                    "warehouse_id": store.warehouse_id,
                    "warehouse": wh.name if wh else None,
                    "catalogue": wh.catalogue.name if (wh and wh.catalogue) else None})
        if warehouse_id and store.warehouse_id != int(warehouse_id):
            continue
        stores.append(row)

    stores.sort(key=lambda r: -r["net"])
    totals = {
        "bills": sum(r["bills"] for r in stores),
        "units": round(sum(r["units"] for r in stores), 3),
        "gross": round(sum(r["gross"] for r in stores), 2),
        "taxable": round(sum(r["taxable"] for r in stores), 2),
        "tax": round(sum(r["tax"] for r in stores), 2),
        "returns": round(sum(r["returns"] for r in stores), 2),
        "net": round(sum(r["net"] for r in stores), 2),
        "stores": len(stores),
    }
    return {
        "available": True, "reason": None, "source": pos_sales.source(),
        "window": {"from": start, "to": end},
        "totals": totals, "stores": stores,
        # Never folded into the totals: a branch the two systems spell
        # differently, and bills raised before the till had branches at all.
        "unmatched": unmatched, "unplaced": unplaced,
        "note": ("Sales are matched to a warehouse through the STORE NAME, which "
                 "is how the till and the warehouse have always been linked. A "
                 "branch listed under “unmatched” is one the two spell "
                 "differently — its money is real and is not in the totals."),
    }


def by_warehouse(db, **kw):
    """The same figures rolled up to the warehouse each store belongs to."""
    res = by_store(db, **kw)
    if not res.get("available"):
        return res
    agg = {}
    for r in res["stores"]:
        a = agg.setdefault(r["warehouse_id"], {
            "warehouse_id": r["warehouse_id"], "warehouse": r["warehouse"],
            "stores": 0, "bills": 0, "units": 0.0, "gross": 0.0,
            "returns": 0.0, "net": 0.0})
        a["stores"] += 1
        for k in ("bills",):
            a[k] += r[k]
        for k in ("units", "gross", "returns", "net"):
            a[k] = round(a[k] + r[k], 3 if k == "units" else 2)
    res["warehouses"] = sorted(agg.values(), key=lambda x: -x["net"])
    return res


def location_ids_for(db, warehouse_id=None):
    """The till's own location ids for our stores, or None for "do not narrow".

    Resolved by NAME, which is the only key the two databases share — see the
    module note. Returns an empty list when a warehouse has stores we cannot
    match, which is a real answer and different from None: it means "this
    warehouse's shops are not recognised down there", and a caller narrowing on
    it correctly gets nothing rather than everything.
    """
    if not pos_sales.available():
        return None
    from .. import models
    q = db.query(models.Store)
    if warehouse_id:
        q = q.filter(models.Store.warehouse_id == int(warehouse_id))
    wanted = {_norm(s.name) for s in q.all()}
    if not warehouse_id:
        return None
    rows = pos_sales._rows("SELECT id, name FROM " + pos_sales.q("locations"), [])
    return [int(lid) for lid, name in rows if _norm(name) in wanted]


def daily(db, days=14, warehouse_id=None, today=None, date_from=None, date_to=None):
    """Net takings per day across the shops — what a trend line is drawn from.

    Every day in the window appears, including the ones nothing sold on. A chart
    built only from days that have rows compresses a closed week into one bar.

    Counts only bills raised AT A BRANCH. A bill with no location — the till had
    none before counters existed — is real money but belongs to no shop, so
    including it here would make the trend disagree with the per-store table
    beneath it. It is reported separately, by `by_store`, as `unplaced`.

    `date_from`/`date_to` (ISO) give the window instead of the last `days`.
    """
    start, end = _window(days, date_from, date_to, today)
    if not pos_sales.available():
        return {"available": False, "labels": [], "net": [], "bills": [], "days": []}

    where, params = pos_sales._window(start, end, "i.invoice_date")
    # …and only this warehouse's shops, when one was named. The parameter was
    # accepted and ignored before, so a scoped dashboard drew a company-wide
    # trend line under figures that were correctly narrowed.
    ids = location_ids_for(db, warehouse_id)
    if ids is not None:
        if not ids:
            d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
            n = (d1 - d0).days + 1
            return {"available": True,
                    "labels": [(d0 + dt.timedelta(days=i)).strftime("%d %b")
                               for i in range(n)],
                    "days": [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)],
                    "net": [0.0] * n, "bills": [0] * n,
                    "window": {"from": start, "to": end}}
        where += " AND i.location_id IN (" + ",".join("?" for _ in ids) + ")"
        params = params + [int(x) for x in ids]
    else:
        where += " AND i.location_id IS NOT NULL"
    states = ",".join("?" for _ in _COUNTED)
    # SUBSTR over the ISO timestamp: the column is TEXT on SQLite and DATETIME on
    # Postgres, and the first ten characters are the date on both. A DATE() cast
    # would be one dialect's spelling.
    rows = pos_sales._rows(
        "SELECT SUBSTR(CAST(i.invoice_date AS CHAR(30)),1,10), "
        "       SUM(i.total), COUNT(DISTINCT i.id) "
        "FROM " + pos_sales.q("invoices") + " i "
        f"WHERE i.payment_status IN ({states})" + where +
        " GROUP BY SUBSTR(CAST(i.invoice_date AS CHAR(30)),1,10)",
        list(_COUNTED) + params)
    got = {str(d): (float(t or 0), int(b or 0)) for d, t, b in rows if d}

    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    labels, net, bills, days_iso = [], [], [], []
    day = d0
    while day <= d1:
        iso = day.isoformat()
        days_iso.append(iso)
        labels.append(day.strftime("%d %b"))
        net.append(round(got.get(iso, (0.0, 0))[0], 2))
        bills.append(got.get(iso, (0.0, 0))[1])
        day += dt.timedelta(days=1)
    return {"available": True, "labels": labels, "net": net, "bills": bills, "days": days_iso,
            "window": {"from": start, "to": end}}
