"""Checks Floor Products (moving goods between floors) and the Comparison Reports.

    python test_floor_products.py

A throwaway database, no warehouse. Two floors of one branch: the ground floor
keeps BOYS and GIRLS wear, the first floor ladies' sarees. Bills are raised on
both floors today and on the ground floor a week ago, stock is received, one
piece comes back. Every figure checked below is worked out by hand from that.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

SHOP_DIR = Path(__file__).resolve().parent
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp()) / 'floor_products_test.db'}"
os.environ["ESSA_WAREHOUSE_DB"] = str(Path(tempfile.mkdtemp()) / "no-warehouse.db")
sys.path.insert(0, str(SHOP_DIR))

from app import create_app, db                                    # noqa: E402
from app import retail_reports as rr                              # noqa: E402
from app.models import (Category, Company, Counter, CreditNote,    # noqa: E402
                        CreditNoteItem, Floor, Invoice, InvoiceItem, Location, Product,
                        ProductFloorMove, StockMovement, TransferReceipt, User)

app = create_app()
failures = []


def check(name, got, want):
    ok_ = got == want
    print(f"{'PASS' if ok_ else 'FAIL'}  {name}")
    if not ok_:
        print(f"        got : {got!r}")
        print(f"        want: {want!r}")
        failures.append(name)


def ok(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        failures.append(name)


now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
today = now.date()
week_ago = now - timedelta(days=7)

with app.app_context():
    db.create_all()
    boss = User(username="boss", full_name="M Anand", role="manager")
    boss.set_password("x")
    till = User(username="till", full_name="Till User", role="cashier")
    till.set_password("x")
    co = Company(name="Taqua Silks")
    boys = Category(name="BOYS-SHIRT", section="KIDS")
    girls = Category(name="GIRLS-FROCK", section="KIDS")
    saree = Category(name="LADIES-SAREE", section="LADIES")
    db.session.add_all([boss, till, co, boys, girls, saree])
    db.session.flush()
    branch = Location(name="TAQUA TIRUPUR", company_id=co.id)
    db.session.add(branch)
    db.session.flush()
    ground = Floor(location_id=branch.id, name="Ground Floor", prefix="TG", sort_order=1)
    first = Floor(location_id=branch.id, name="First Floor", prefix="TF", sort_order=2)
    db.session.add_all([ground, first])
    db.session.flush()
    g_till = Counter(name="G-BILL1", location_id=branch.id, floor_id=ground.id)
    f_till = Counter(name="F-BILL1", location_id=branch.id, floor_id=first.id)
    db.session.add_all([g_till, f_till])
    shirt = Product(sku="B-001", name="BOYS CHECK SHIRT", category_id=boys.id, cost_price=300.0,
                    selling_price=600.0, gst_rate=5.0, stock_qty=10, floor_id=ground.id)
    shirt2 = Product(sku="B-002", name="BOYS DENIM SHIRT", category_id=boys.id, cost_price=400.0,
                     selling_price=800.0, gst_rate=5.0, stock_qty=4, floor_id=ground.id)
    frock = Product(sku="G-001", name="GIRLS PARTY FROCK", category_id=girls.id, cost_price=500.0,
                    selling_price=1000.0, gst_rate=5.0, stock_qty=6, floor_id=ground.id)
    silk = Product(sku="L-001", name="SILK SAREE", category_id=saree.id, cost_price=2000.0,
                   selling_price=4000.0, gst_rate=5.0, stock_qty=3, floor_id=first.id)
    loose = Product(sku="X-001", name="LOOSE ITEM", cost_price=50.0, selling_price=100.0, stock_qty=2)
    db.session.add_all([shirt, shirt2, frock, silk, loose])
    db.session.flush()

    def bill(number, when, floor, counter, lines):
        sub = sum(q * p for _, q, p in lines)
        tax = round(sub * 0.05, 2)
        inv = Invoice(invoice_number=number, invoice_date=when, cashier_id=till.id, subtotal=sub,
                      cgst=tax / 2, sgst=tax / 2, total=sub + tax, payment_method="cash",
                      company_id=co.id, location_id=branch.id, counter_id=counter.id, floor_id=floor.id)
        db.session.add(inv)
        db.session.flush()
        for prod, q, p in lines:
            db.session.add(InvoiceItem(invoice_id=inv.id, product_id=prod.id, quantity=q, unit_price=p,
                                       gst_rate=5.0, line_total=q * p, tax_amount=round(q * p * 0.05, 2)))
        return inv

    # today: ground sells 2 boys shirts (1,260 with tax) and a frock (1,050); first sells a saree (4,200)
    a = bill("TG26-001", now, ground, g_till, [(shirt, 2, 600.0), (frock, 1, 1000.0)])
    bill("TF26-001", now, first, f_till, [(silk, 1, 4000.0)])
    # a week ago: the ground floor sold one denim shirt (840)
    bill("TG26-000", week_ago, ground, g_till, [(shirt2, 1, 800.0)])
    db.session.flush()
    # today one boys shirt comes back
    cn = CreditNote(number="CN-1", invoice_id=a.id, cashier_id=till.id, created_at=now, subtotal=600.0,
                    cgst=15.0, sgst=15.0, total=630.0, refund_method="cash")
    db.session.add(cn)
    db.session.flush()
    db.session.add(CreditNoteItem(credit_note_id=cn.id, invoice_item_id=a.items[0].id, product_id=shirt.id,
                                  quantity=1, unit_price=600.0, gst_rate=5.0, line_total=600.0, tax_amount=30.0))
    # received today: 5 boys shirts from the warehouse, 2 sarees as an opening entry
    db.session.add(TransferReceipt(wh_line_id=1, code="TRF-1", location_id=branch.id, product_id=shirt.id,
                                   qty=5, applied_at=now))
    db.session.add(StockMovement(product_id=silk.id, change=2, reason="opening", created_at=now))
    db.session.commit()
    ids = {p.sku: p.id for p in Product.query.all()}
    floor_id = {"ground": ground.id, "first": first.id}
    boss_id, till_id = boss.id, till.id


def run(key, start=today, end=today, **params):
    with app.app_context():
        return rr.run(key, start, end, params=params)


def col(res, name):
    return res["columns"].index(name)


print("-- Floor wise Sales")
r = run("floor_sales")
check("each floor, the highest first", [row[0] for row in r["rows"]], ["First Floor", "Ground Floor"])
check("…with its bills and sales (tax in)", [(row[1], row[3]) for row in r["rows"]], [(1, 4200.0), (1, 2310.0)])
check("…and the ground floor's best seller", r["rows"][1][col(r, "Best seller")], "BOYS CHECK SHIRT")
check("the highest floor named in the totals", r["totals"]["Highest floor"], "First Floor (64.5%)")

print("\n-- Product wise Sales")
r = run("product_sales", start=today - timedelta(days=1), end=today)
check("yesterday and today, best first, ranked", [(row[0], row[1]) for row in r["rows"]],
      [(1, "L-001"), (2, "B-001"), (3, "G-001")])
check("the best seller in the totals", r["totals"]["Best seller"], "SILK SAREE")
r = run("product_sales", floor=str(floor_id["ground"]))
check("narrowed to one floor's products", [row[1] for row in r["rows"]], ["B-001", "G-001"])

print("\n-- Sales Vs Purchase - Floor wise")
r = run("floor_sales_purchase")
g = next(row for row in r["rows"] if row[0] == "Ground Floor")
f = next(row for row in r["rows"] if row[0] == "First Floor")
check("ground: 5 received worth 1,500 at cost, 3 sold for 2,310, 1 back, 20 held",
      [g[col(r, k)] for k in ("Received", "Received value", "Sold", "Sales", "Returned", "In stock now")],
      [5, 1500.0, 3, 2310.0, 1, 20])
check("first: 2 received worth 4,000, 1 sold for 4,200, 3 held worth 6,000",
      [f[col(r, k)] for k in ("Received", "Received value", "Sold", "Sales", "In stock now", "Stock value")],
      [2, 4000.0, 1, 4200.0, 3, 6000.0])
ok("goods on no floor are their own row", any(row[0] == "No floor" for row in r["rows"]))

print("\n-- Sales Vs Purchase - Product wise")
r = run("product_sales_purchase")
check("only products with something in the period",
      sorted(row[0] for row in r["rows"]), ["B-001", "G-001", "L-001"])
row = next(x for x in r["rows"] if x[0] == "B-001")
check("the boys shirt: 5 in, 2 sold, 1 back, 10 held",
      [row[col(r, k)] for k in ("Received", "Sold", "Returned", "In stock now")], [5, 2, 1, 10])

print("\n-- Period comparisons")
r = run("floor_compare", start=today - timedelta(days=6), end=today)
g = next(x for x in r["rows"] if x[0] == "Ground Floor")
check("ground floor: this week against the week before", (g[1], g[2], g[3]), (2310.0, 840.0, 1470.0))
check("…as a percentage", g[4], 175.0)
f = next(x for x in r["rows"] if x[0] == "First Floor")
check("a floor that sold nothing before is new", f[4], "new")
r = run("product_compare", start=today - timedelta(days=6), end=today)
row = next(x for x in r["rows"] if x[0] == "B-002")
check("a product that stopped selling", (row[col(r, "Sales now")], row[col(r, "Sales then")], row[col(r, "Change %")]),
      (0.0, 840.0, -100.0))
r = run("floor_compare", compare="last_year")
ok("compared with the same dates last year", str(today.year - 1) in r["note"])

print("\n-- the report pages")
client = app.test_client()
with client.session_transaction() as s:
    s["_user_id"] = str(boss_id)
    s["_fresh"] = True
html = client.get("/reports/r/product_sales").get_data(as_text=True)
ok("the floor picker lists the floors", "Ground Floor" in html and "Not on a floor" in html)
ok("one click for yesterday", ">Yesterday</a>" in html)
for key in ("floor_sales", "product_sales", "floor_sales_purchase", "product_sales_purchase",
            "floor_compare", "product_compare"):
    check(f"{key} opens and exports", (client.get(f"/reports/r/{key}").status_code,
                                       client.get(f"/reports/r/{key}?export=csv").status_code), (200, 200))

print("\n-- Floor Products")
page = client.get(f"/floor-products/?floor={floor_id['ground']}")
html = page.get_data(as_text=True)
check("the screen opens", page.status_code, 200)
ok("each floor is a tile, with the ones on no floor", "Ground Floor" in html and "Not on a floor" in html)
ok("the ground floor's categories, by section", "BOYS-SHIRT" in html and "GIRLS-FROCK" in html and "KIDS" in html)
ok("its products with their stock", "BOYS CHECK SHIRT" in html and "SILK SAREE" not in html.split("Products on")[1])
html = client.get(f"/floor-products/?floor={floor_id['ground']}&section=KIDS&q=denim").get_data(as_text=True)
ok("narrowed by name", "BOYS DENIM SHIRT" in html and "BOYS CHECK SHIRT" not in html.split("Products on")[1])

back = f"/floor-products/?floor={floor_id['ground']}"
r = client.post("/floor-products/move", data={"ids": [ids["G-001"]], "to_floor": str(floor_id["first"]),
                                               "floor": str(floor_id["ground"]), "back": back})
check("moving a ticked product sends you back to the list", r.status_code, 302)
with app.app_context():
    check("the frock is on the first floor now", db.session.get(Product, ids["G-001"]).floor_id, floor_id["first"])

r = client.post("/floor-products/move", data={"scope": "all", "only_category": "BOYS-SHIRT",
                                               "to_floor": str(floor_id["first"]), "floor": str(floor_id["ground"]),
                                               "back": back, "note": "Kids wear upstairs"})
with app.app_context():
    moved = [p.sku for p in Product.query.filter_by(floor_id=floor_id["first"]).order_by(Product.sku).all()]
    check("every boys shirt goes with one move", moved, ["B-001", "B-002", "G-001", "L-001"])
    batch = ProductFloorMove.query.filter_by(note="Kids wear upstairs").all()
    check("…written down as one batch, from the ground floor", (len(batch), {m.from_floor_id for m in batch}),
          (2, {floor_id["ground"]}))
    check("…without touching a quantity", db.session.get(Product, ids["B-001"]).stock_qty, 10)
    batch_code = batch[0].batch

html = client.get(f"/floor-products/?floor={floor_id['first']}").get_data(as_text=True)
ok("the moves are listed with who made them", "Recent moves" in html and "M Anand" in html and "Kids wear upstairs" in html)

r = client.post(f"/floor-products/undo/{batch_code}", data={"back": back})
with app.app_context():
    check("putting a move back returns the shirts downstairs",
          sorted(p.sku for p in Product.query.filter_by(floor_id=floor_id["ground"]).all()), ["B-001", "B-002"])
    check("…and leaves the frock, which was a different move",
          db.session.get(Product, ids["G-001"]).floor_id, floor_id["first"])
    ok("the batch is marked put back", all(m.undone_by for m in ProductFloorMove.query.filter_by(batch=batch_code)))

r = client.post("/floor-products/move", data={"ids": [ids["X-001"]], "to_floor": str(floor_id["first"]),
                                               "floor": "none", "back": "/pos/floor-products/?floor=none"})
check("it returns to the page it came from, mount prefix and all", r.headers["Location"].endswith("/pos/floor-products/?floor=none"), True)
r = client.post("/floor-products/move", data={"ids": [ids["X-001"]], "to_floor": str(floor_id["ground"]),
                                               "floor": "none", "back": "//evil.example/x"})
check("…but never to another site", r.headers["Location"].endswith("/floor-products/") and "evil" not in r.headers["Location"], True)
with app.app_context():
    db.session.get(Product, ids["X-001"]).floor_id = None
    db.session.commit()
html = client.get("/floor-products/?floor=none").get_data(as_text=True)
ok("the page's own return address carries the mount's prefix", 'name="back" value="/floor-products/?floor=none"' in html)

r = client.post("/floor-products/move", data={"ids": [ids["X-001"]], "to_floor": "", "floor": "none", "back": back})
with app.app_context():
    check("no floor chosen, nothing moves", db.session.get(Product, ids["X-001"]).floor_id, None)

print("\n-- Stock Check says where an item is kept")
html = client.get("/stock-check/?code=G-001").get_data(as_text=True)
ok("Kept on First Floor", "Kept on" in html and "First Floor" in html)

print("\n-- a cashier can look, not move")
cashier = app.test_client()
with cashier.session_transaction() as s:
    s["_user_id"] = str(till_id)
    s["_fresh"] = True
check("the screen opens for them", cashier.get("/floor-products/").status_code, 200)
check("moving is refused", cashier.post("/floor-products/move", data={"ids": [ids["L-001"]],
                                                                      "to_floor": str(floor_id["ground"])}).status_code, 403)
ok("and they are not offered the move bar", "Move ticked" not in cashier.get("/floor-products/").get_data(as_text=True))

print()
print("=" * 60)
if failures:
    print(f"{len(failures)} FAILED:")
    for f_ in failures:
        print("  -", f_)
    sys.exit(1)
print("All floor product checks passing.")
