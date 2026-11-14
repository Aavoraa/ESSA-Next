"""Checks settling several of one customer's bills with one payment.

    python test_settle_many.py

Runs against a throwaway database with no warehouse attached, so it never
touches textile_shop.db.

A customer who bought on three trips and pays once must leave with all three
bills paid, each for exactly its own total, the change on the one cash really
went to — and a payment that does not add up must pay none of them, not some.
"""
import os
import sys
import tempfile
from pathlib import Path

SHOP_DIR = Path(__file__).resolve().parent
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp()) / 'settle_many_test.db'}"
os.environ["ESSA_WAREHOUSE_DB"] = str(Path(tempfile.mkdtemp()) / "no-warehouse.db")
os.environ.pop("MESSAGE_WEBHOOK_URL", None)
sys.path.insert(0, str(SHOP_DIR))

from app import create_app, db                                       # noqa: E402
from app.models import (Category, Company, Counter, Customer,        # noqa: E402
                        Invoice, InvoicePayment, Location, Product, User)

app = create_app()
failures = []


def check(name, got, want):
    ok_ = got == want
    print(f"{'PASS' if ok_ else 'FAIL'}  {name}")
    if not ok_:
        print(f"        got : {got!r}")
        print(f"        want: {want!r}")
        failures.append(name)


def ok(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(name)


with app.app_context():
    db.create_all()
    boss = User(username="boss", full_name="M Anand", role="manager")
    boss.set_password("x")
    till = User(username="till", full_name="Till User", role="cashier")
    till.set_password("x")
    cat = Category(name="MENS-SHIRT", section="MENS")
    kumar = Customer(name="R Kumar", phone="9876500001")
    meena = Customer(name="S Meena", phone="9876500002")
    co = Company(name="Taqua Silks", gstin="33AAAAA0000A1Z5")
    db.session.add_all([boss, till, cat, kumar, meena, co])
    db.session.flush()
    branch = Location(name="TAQUA TIRUPUR", company_id=co.id)
    db.session.add(branch)
    db.session.flush()
    counter = Counter(name="G-PAY1", location_id=branch.id)
    shirt = Product(sku="ESSA-00002", name="MENS COTTON SHIRT", category_id=cat.id,
                    selling_price=1000.0, cost_price=500.0, gst_rate=5.0, stock_qty=50)
    db.session.add_all([counter, shirt])
    db.session.commit()
    IDS = {"boss": boss.id, "kumar": kumar.id, "meena": meena.id, "branch": branch.id,
           "counter": counter.id, "shirt": shirt.id, "co": co.id}

client = app.test_client()
with client.session_transaction() as s:
    s["_user_id"] = str(IDS["boss"])
    s["_fresh"] = True
client.post("/pos/place", json={"company_id": IDS["co"], "location_id": IDS["branch"],
                                "floor_id": None, "counter_id": IDS["counter"]})
client.post("/drawer/open", data={"opening_float": "500"})


def raise_bill(customer):
    r = client.post("/pos/checkout", json={
        "settle_later": True, "staff_code": "till", "customer_id": IDS[customer],
        "items": [{"product_id": IDS["shirt"], "quantity": 1, "unit_price": 1000.0}]})
    j = r.get_json()
    assert j.get("invoice_id"), j
    return j["invoice_id"]


def statuses(ids):
    with app.app_context():
        return [db.session.get(Invoice, i).payment_status for i in ids]


print("\n-- three bills for one customer, one for another")
k1, k2, k3 = raise_bill("kumar"), raise_bill("kumar"), raise_bill("kumar")
m1 = raise_bill("meena")
check("all four wait for payment", statuses([k1, k2, k3, m1]), ["pending"] * 4)

page = client.get(f"/pos/settle?id={k1}").get_data(as_text=True)
with app.app_context():
    nums = {i: db.session.get(Invoice, i).invoice_number for i in (k1, k2, k3, m1)}
ok("Settlement offers the customer's other bills", nums[k2] in page and nums[k3] in page
   and "settle together" in page)
ok("…and not another customer's", f'value="{m1}"' not in page)

print("\n-- refusals pay nothing")
r = client.post("/pos/settle-many", json={"invoice_ids": [k1, k2],
                                          "payments": [{"method": "cash", "amount": 1000}]})
ok("a short payment is refused", r.status_code == 400 and "short" in r.get_json()["error"],
   r.get_json().get("error"))
r = client.post("/pos/settle-many", json={"invoice_ids": [k1, m1],
                                          "payments": [{"method": "cash", "amount": 2100}]})
ok("two customers' bills are refused", r.status_code == 400)
r = client.post("/pos/settle-many", json={"invoice_ids": [k1],
                                          "payments": [{"method": "cash", "amount": 1050}]})
ok("one bill is not 'together'", r.status_code == 400)
check("nothing was paid by any of those", statuses([k1, k2, k3, m1]), ["pending"] * 4)

print("\n-- one payment, card and cash with change, settles all three")
r = client.post("/pos/settle-many", json={
    "invoice_ids": [k1, k2, k3],
    "payments": [{"method": "card", "amount": 1000, "tendered": 1000, "reference": "4242"},
                 {"method": "cash", "amount": 2150, "tendered": 2200}]})
j = r.get_json()
ok("settled", r.status_code == 200 and j.get("success"), j.get("error"))
check("the three bills, oldest first", j.get("invoice_ids"), [k1, k2, k3])
check("all three are paid; the other customer's is not", statuses([k1, k2, k3, m1]),
      ["paid", "paid", "paid", "pending"])
with app.app_context():
    each = {i: round(sum(p.amount for p in InvoicePayment.query.filter_by(invoice_id=i)), 2)
            for i in (k1, k2, k3)}
    check("each bill is paid exactly its own total", each, {k1: 1050.0, k2: 1050.0, k3: 1050.0})
    first = InvoicePayment.query.filter_by(invoice_id=k1).all()
    check("card goes first, so the first bill carries it",
          sorted((p.method, p.amount) for p in first), [("card", 1000.0), ("cash", 50.0)])
    last_cash = InvoicePayment.query.filter_by(invoice_id=k3, method="cash").one()
    check("the change is on the last bill's cash", round(last_cash.tendered - last_cash.amount, 2), 50.0)
check("the till is told the change", j.get("change"), 50.0)

r = client.post("/pos/settle-many", json={"invoice_ids": [k1, k2],
                                          "payments": [{"method": "cash", "amount": 2100}]})
ok("paid bills cannot be paid again", r.status_code == 400 and "already settled" in r.get_json()["error"])

print("\n-- the customer copies print one after another")
html = client.get(f"/pos/invoice/{k1}/print?copy=customer&fresh=1&next={k2},{k3}").get_data(as_text=True)
ok("the first copy goes on to the second", f"/pos/invoice/{k2}/print" in html and f"next={k3}" in html)

print("\n-- a Billing till cannot take money for several bills either")
k4, k5 = raise_bill("kumar"), raise_bill("kumar")
with app.app_context():
    db.session.get(Counter, IDS["counter"]).role = "billing"
    db.session.commit()
r = client.post("/pos/settle-many", json={"invoice_ids": [k4, k5],
                                          "payments": [{"method": "cash", "amount": 2100}]})
ok("refused at a Billing till", r.status_code == 400 and "Billing till" in r.get_json()["error"])
check("and nothing was paid", statuses([k4, k5]), ["pending", "pending"])

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("All settle-together checks passing.")
