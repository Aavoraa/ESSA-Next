"""Checks the CRM module — every screen opens, and the parts that touch money add up.

    python test_crm.py

No pytest — the shop has no test dependency and this needs none. Runs against a
throwaway database with no warehouse attached, so it never touches
textile_shop.db.

Most of the CRM is screens over records, and for those it is enough that each
opens and saves. The checks that matter most are where it meets the till: a gift
voucher is money, so it must spend down and refuse to over-spend; points turned
into a coupon must leave the balance; a price list must reach the counter; a
customer with loyalty switched off must earn nothing; and cash taken for
vouchers must be in the drawer's count.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SHOP_DIR = Path(__file__).resolve().parent
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp()) / 'crm_test.db'}"
os.environ["ESSA_WAREHOUSE_DB"] = str(Path(tempfile.mkdtemp()) / "no-warehouse.db")
os.environ.pop("MESSAGE_WEBHOOK_URL", None)
sys.path.insert(0, str(SHOP_DIR))

from itsdangerous import URLSafeSerializer                           # noqa: E402

from app import create_app, db, drawer, modules, vouchers             # noqa: E402
from app.models import (Category, Coupon, Customer, CustomerAdvance,  # noqa: E402
                        CustomerFeedback, CustomerOrder, CustomerVisit, DrawerSession,
                        FeedbackAnswer, FeedbackQuestion, GiftVoucher, GiftVoucherBatch,
                        Invoice, InvoiceItem, Lead, LoyaltyTxn, PriceCategory, PriceListItem,
                        Product, ScheduledMessage, User)

app = create_app()
app.config["WTF_CSRF_ENABLED"] = False
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


# ---- a shop with a customer, two products and a paid bill ------------------
with app.app_context():
    db.create_all()
    boss = User(username="boss", full_name="M Anand", role="manager")
    boss.set_password("x")
    till = User(username="till", full_name="Till User", role="cashier")
    till.set_password("x")
    cat = Category(name="SAREE")
    kumar = Customer(name="R Kumar", phone="9876500001", city="Tirupur", category="Regular",
                     dob=date(1990, date.today().month, min(date.today().day, 28)),
                     loyalty_points=120)
    db.session.add_all([boss, till, cat, kumar])
    db.session.flush()
    silk = Product(sku="ESSA-00011", name="SILK SAREE", category_id=cat.id, selling_price=2000.0,
                   gst_rate=5.0, stock_qty=20, design_no="D-501", brand="Kanchi", size="FREE")
    cotton = Product(sku="ESSA-00012", name="COTTON SAREE", category_id=cat.id, selling_price=800.0,
                     gst_rate=5.0, stock_qty=20, design_no="D-777")
    db.session.add_all([silk, cotton])
    db.session.flush()
    paid = Invoice(invoice_number="INV-000201", customer_id=kumar.id, cashier_id=boss.id,
                   subtotal=2000, total=2100, payment_status="paid",
                   invoice_date=datetime.utcnow() - timedelta(days=2))
    db.session.add(paid)
    db.session.flush()
    db.session.add(InvoiceItem(invoice_id=paid.id, product_id=silk.id, quantity=1, unit_price=2000,
                               gst_rate=5.0, line_total=2000))
    db.session.commit()
    KUMAR, SILK, COTTON, PAID = kumar.id, silk.id, cotton.id, paid.id

client = app.test_client()


def login(username):
    with app.app_context():
        u = User.query.filter_by(username=username).first()
    with client.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True


def pending_bill(number, total, customer_id=None):
    with app.app_context():
        inv = Invoice(invoice_number=number, customer_id=customer_id, cashier_id=1, subtotal=total,
                      total=total, payment_status="pending")
        db.session.add(inv)
        db.session.commit()
        return inv.id


login("boss")

# ---- every CRM screen opens --------------------------------------------------
print("\n-- every CRM screen in the menu opens")
crm_keys = [m["key"] for m in modules.MODULES]
start = crm_keys.index("customers")
crm_mods = modules.MODULES[start:start + 15]
check("fifteen CRM entries follow the customer master", len(crm_mods), 15)
check("Bill Print is gone", [m["key"] for m in modules.MODULES if m["key"] == "bill_print"], [])
with app.test_request_context():
    from flask import url_for
    urls = [(m["label"], url_for(m["endpoint"])) for m in crm_mods]
for label, url in urls:
    r = client.get(url)
    ok(f"{label} ({url})", r.status_code == 200, f"HTTP {r.status_code}")

extra = ["/customers/new", f"/customers/{KUMAR}", f"/customers/{KUMAR}/edit", "/orders/new",
         "/gift-vouchers/history?q=GV0", "/gift-vouchers/search", "/coupons/history",
         "/coupons/rewards?mobile=9876500001", "/coupons/print", "/crm-feedback/form/preview",
         "/leads/new", "/registration/poster",
         "/loyalty/?mobile=9876&summary=", "/send-sms/?run=1", "/send-sms/?source=leads&run=1",
         "/crm-analyser/?from=2020-01-01", "/orders/export.csv", "/loyalty/export.csv",
         "/crm-feedback/report.csv"]
for url in extra:
    r = client.get(url)
    ok(f"opens {url}", r.status_code == 200, f"HTTP {r.status_code}")

print("\n-- the CRM is Central's menu, not the Store's")
r = client.get("/")
ok("the Store Dashboard offers no CRM-only screen", b"Gift Voucher" not in r.data and b"Lead Management" not in r.data)
ok("…but still Customers and Promotions", b"Customers" in r.data and b"Promotions" in r.data)
r = client.get("/leads/")
ok("on a CRM screen the menu is the CRM", b"Gift Voucher" in r.data and b"Billing Counter" not in r.data)

# ---- customer master -------------------------------------------------------
print("\n-- the customer master takes the full profile, and refuses a duplicate mobile")
r = client.post("/customers/new", data={
    "name": "S Priya", "phone": "9000000002", "customer_type": "wholesale", "category": "VIP",
    "gender": "female", "married": "on", "kids_boys": "1", "kids_girls": "2", "city": "Erode",
    "area": "RS Puram", "pincode": "638001", "religion": "Hindu", "pan": "abcde1234f",
    "credit_allowed": "on", "credit_days": "30", "credit_limit": "50000", "active": "on"},
    follow_redirects=False)
check("saved and opened", r.status_code, 302)
with app.app_context():
    priya = Customer.query.filter_by(phone="9000000002").first()
    check("type", priya.customer_type, "wholesale")
    check("kids", (priya.kids_boys, priya.kids_girls), (1, 2))
    check("PAN upper-cased", priya.pan, "ABCDE1234F")
    check("credit", (priya.credit_allowed, priya.credit_days, priya.credit_limit), (True, 30, 50000.0))
    check("code given when blank", priya.code, priya.card_code)
    check("active", bool(priya.inactive), False)
    PRIYA = priya.id
r = client.post("/customers/new", data={"name": "Someone Else", "phone": "9000000002", "active": "on"})
check("a second customer on the same mobile is refused", r.status_code, 200)
ok("…saying who has it", b"S Priya already has the mobile number" in r.data)

# ---- customer orders ---------------------------------------------------------
print("\n-- an order with an advance; the advance is the customer's to spend at the till")
r = client.post("/orders/new", data={
    "customer_phone": "9876500001", "customer_name": "R Kumar", "customer_id": str(KUMAR),
    "delivery_date": (date.today() + timedelta(days=10)).isoformat(),
    "line_description": ["Blouse stitching", "Saree fall"], "line_design": ["", ""],
    "line_size": ["38", ""], "line_color": ["", ""], "line_qty": ["2", "1"], "line_rate": ["450", "150"],
    "advance": "500", "method": "cash"})
check("taken", r.status_code, 302)
with app.app_context():
    o = CustomerOrder.query.first()
    check("two lines", len(o.lines), 2)
    check("order value", o.total_amount, 1050.0)
    check("paid", o.paid_amount, 500.0)
    check("balance", o.balance, 550.0)
    check("the advance carries the order", CustomerAdvance.query.filter_by(order_id=o.id).count(), 1)
    check("it is spendable as the customer's advance", vouchers.customer_advance_balance(KUMAR), 500.0)
    OID = o.id
r = client.post(f"/orders/{OID}/pay", data={"amount": "550", "method": "upi"})
with app.app_context():
    check("paid in full", db.session.get(CustomerOrder, OID).balance, 0.0)
r = client.post(f"/orders/{OID}/status", data={"status": "ready"})
with app.app_context():
    check("moved on", db.session.get(CustomerOrder, OID).status, "ready")
r = client.post("/orders/new", data={"customer_phone": "9111111111", "customer_name": "New Walkin",
                                      "line_description": [""], "line_qty": ["1"], "line_rate": ["0"]})
check("an order with no items is refused", r.status_code, 400)
with app.app_context():
    check("…and nobody was added for it", Customer.query.filter_by(phone="9111111111").count(), 0)
r = client.get(f"/orders/{OID}")
check("order page opens", r.status_code, 200)
r = client.get(f"/orders/{OID}/print")
check("order slip prints", r.status_code, 200)
r = client.get("/orders/?status=all&q=Kumar")
ok("listed under the customer", b"ORD-" in r.data)

# ---- gift vouchers ---------------------------------------------------------
print("\n-- gift vouchers: sold by denomination, spent at Settlement, never over-spent")
r = client.post("/gift-vouchers/", data={"mode": "direct", "customer_phone": "9876500001",
                                          "customer_name": "R Kumar", "customer_id": str(KUMAR),
                                          "d500": "2", "d1000": "1", "method": "cash", "amount": "1999"})
with app.app_context():
    check("an amount that does not match the vouchers is refused", GiftVoucherBatch.query.count(), 0)
r = client.post("/gift-vouchers/", data={"mode": "direct", "customer_phone": "9876500001",
                                          "customer_name": "R Kumar", "customer_id": str(KUMAR),
                                          "d500": "2", "d1000": "1", "method": "cash", "amount": "2000",
                                          "given_to": "Meena"})
check("sold", r.status_code, 302)
with app.app_context():
    b = GiftVoucherBatch.query.first()
    check("three vouchers", len(b.vouchers), 3)
    check("face value", b.face_value, 2000.0)
    v1000 = next(v for v in b.vouchers if v.value == 1000)
    CODE = v1000.code
    r = client.get(f"/gift-vouchers/batch/{b.id}")
    check("voucher sheet prints", r.status_code, 200)
r = client.get(f"/pos/api/gift-voucher?code={CODE.lower()}")
check("the till finds it, any case", (r.get_json()["ok"], r.get_json()["balance"]), (True, 1000.0))

bill = pending_bill("INV-000301", 700.0)
r = client.post(f"/pos/settle/{bill}", json={"payments": [
    {"method": "gift_voucher", "amount": 700, "tendered": 700, "reference": CODE}]})
check("spent 700 at Settlement", r.status_code, 200)
with app.app_context():
    check("300 left on it", vouchers.gift_voucher_balance(GiftVoucher.query.filter_by(code=CODE).first()), 300.0)
bill2 = pending_bill("INV-000302", 500.0)
r = client.post(f"/pos/settle/{bill2}", json={"payments": [
    {"method": "gift_voucher", "amount": 500, "tendered": 500, "reference": CODE}]})
check("spending more than is left is refused", r.status_code, 400)
ok("…with what is left", "300.00" in (r.get_json() or {}).get("error", ""), r.get_json())
r = client.post(f"/pos/settle/{bill2}", json={"payments": [
    {"method": "gift_voucher", "amount": 300, "tendered": 300, "reference": CODE},
    {"method": "cash", "amount": 200, "tendered": 200}]})
check("the rest of it, plus cash", r.status_code, 200)
r = client.get(f"/pos/api/gift-voucher?code={CODE}")
check("now spent in full", r.get_json()["ok"], False)
r = client.get(f"/gift-vouchers/history?q={CODE}")
ok("history shows both bills", b"INV-000301" in r.data and b"INV-000302" in r.data)
with app.app_context():
    check("expired voucher refused", vouchers.gift_voucher_problem(
        GiftVoucher.query.filter_by(code=CODE).first(), today=date.today() + timedelta(days=400)) is not None, True)
    # cash taken for vouchers is in the drawer
    s = DrawerSession(counter_id=None, opening_float=0, opened_at=datetime.utcnow() - timedelta(hours=1),
                      opened_by_id=1)
    db.session.add(s)
    db.session.commit()
    _, parts = drawer.breakdown(s)
    check("the drawer counts voucher cash", parts.get("Gift vouchers sold for cash"), 2000.0)

r = client.post("/gift-vouchers/", data={"mode": "complimentary", "d100": "3"})
with app.app_context():
    cb = GiftVoucherBatch.query.filter_by(mode="complimentary").first()
    check("complimentary vouchers need no buyer or money", (cb is not None, cb.amount if cb else None), (True, 0.0))

# ---- rewards to coupon -------------------------------------------------------
print("\n-- loyalty points turned into a coupon leave the balance, and the coupon is worth them")
r = client.post("/coupons/rewards", data={"customer_id": str(KUMAR), "mode": "multiple", "value": "50",
                                          "count": "3"})
with app.app_context():
    check("150 points is more than 120 — refused", Coupon.query.filter_by(issued_via="loyalty").count(), 0)
r = client.post("/coupons/rewards", data={"customer_id": str(KUMAR), "mode": "single", "value": "100"})
check("made", r.status_code, 302)
with app.app_context():
    cp = Coupon.query.filter_by(issued_via="loyalty").first()
    check("worth its own value", cp.value, 100.0)
    check("points left", db.session.get(Customer, KUMAR).loyalty_points, 20.0)
    check("on the ledger", LoyaltyTxn.query.filter(LoyaltyTxn.points == -100).count(), 1)
    check("the till takes ₹100 off", vouchers.coupon_amount(cp, 1000, customer_id=KUMAR), 100.0)
    CP = cp.code
r = client.get(f"/coupons/print?code={CP}")
ok("Settlement Coupon Print finds it", CP.encode() in r.data)
r = client.post("/coupons/issue", data={"campaign_id": "1", "count": "1"})
with app.app_context():
    check("nobody can issue ₹0 reward coupons by hand", Coupon.query.count(), 1)

# ---- price list ------------------------------------------------------------
print("\n-- a price list reaches the counter")
client.post("/price-list/category", data={"name": "Wholesale", "discount_pct": "10"})
with app.app_context():
    pc = PriceCategory.query.filter_by(name="Wholesale").first()
    PC = pc.id
client.post(f"/price-list/{PC}/prices", data={"new_design": "D-501", "new_price": "1700"})
client.post(f"/price-list/{PC}/customers", data={"customer": "9000000002"})
with app.app_context():
    check("design priced", PriceListItem.query.filter_by(category_id=PC).first().price, 1700.0)
    check("Priya is on it", db.session.get(Customer, PRIYA).price_category_id, PC)
r = client.get(f"/pos/api/product?code=ESSA-00011&customer_id={PRIYA}")
check("the priced design sells at its list price", r.get_json()["price"], 1700.0)
r = client.get(f"/pos/api/product?code=ESSA-00012&customer_id={PRIYA}")
check("anything else at the list's discount", r.get_json()["price"], 720.0)
r = client.get("/pos/api/product?code=ESSA-00012")
check("without the customer, the ordinary price", r.get_json()["price"], 800.0)
r = client.get(f"/pos/api/list-prices?ids={SILK},{COTTON}&customer_id={PRIYA}")
check("lines already on the bill are repriced", r.get_json()["prices"], {str(SILK): 1700.0, str(COTTON): 720.0})
r = client.get(f"/price-list/?cat={PC}")
ok("the list shows the design and the customer", b"D-501" in r.data and b"S Priya" in r.data)

# ---- loyalty switched off --------------------------------------------------
print("\n-- a customer with loyalty switched off earns nothing")
with app.app_context():
    p = db.session.get(Customer, PRIYA)
    p.loyalty_disabled = True
    db.session.commit()
big = pending_bill("INV-000303", 5000.0, PRIYA)
client.post(f"/pos/settle/{big}", json={"payments": [{"method": "card", "amount": 5000, "tendered": 5000}]})
with app.app_context():
    check("no points", db.session.get(Customer, PRIYA).loyalty_points or 0, 0)
    check("but the spend is counted", db.session.get(Customer, PRIYA).total_spent, 5000.0)

# ---- loyalty management ----------------------------------------------------
print("\n-- loyalty: lookup and a manager's correction")
r = client.get("/loyalty/?mobile=9876500001&summary=1")
ok("found by mobile", b"R Kumar" in r.data)
client.post("/loyalty/adjust", data={"customer_id": str(KUMAR), "points": "-50", "reason": "test"})
with app.app_context():
    check("cannot go below zero", db.session.get(Customer, KUMAR).loyalty_points, 20.0)
client.post("/loyalty/adjust", data={"customer_id": str(KUMAR), "points": "30", "reason": "missed bill"})
with app.app_context():
    check("added with a reason", db.session.get(Customer, KUMAR).loyalty_points, 50.0)

# ---- send sms ----------------------------------------------------------------
print("\n-- Send SMS picks by what was bought and queues one message per number")
r = client.get("/send-sms/?run=1&brand=Kanchi")
ok("Kanchi buyers: Kumar", b"R Kumar" in r.data and b"S Priya" not in r.data)
r = client.get("/send-sms/?run=1&city=Erode")
ok("Erode: Priya", b"S Priya" in r.data and b"R Kumar" not in r.data)
today = date.today()
r = client.get(f"/send-sms/?run=1&dob_from={today.replace(day=1).isoformat()}&dob_to={today.isoformat()}")
ok("birthdays this month: Kumar", b"R Kumar" in r.data)
r = client.get(f"/send-sms/?run=1&exclude_from={(today - timedelta(days=7)).isoformat()}&exclude_to={today.isoformat()}")
ok("bought this week are left out", b"R Kumar" not in r.data)
r = client.post("/send-sms/send", data={"ids": [str(KUMAR), str(PRIYA)], "extra": ["9123456789", "bad"],
                                        "message": "Hi {name}, new silks are in.", "now": "1"})
with app.app_context():
    msgs = ScheduledMessage.query.filter_by(kind="offer").all()
    check("three messages, the bad number dropped", len(msgs), 3)
    check("named", sorted(m.body for m in msgs)[0].startswith("Hi "), True)
    check("logged — there is no gateway", {m.status for m in msgs}, {"logged"})

# ---- feedback ----------------------------------------------------------------
print("\n-- feedback: questions on the bill's QR page, answers on the dashboard")
client.post("/crm-feedback/questions/save", data={"text": "How helpful was our staff?", "kind": "rating",
                                                  "required": "on"})
client.post("/crm-feedback/questions/save", data={"text": "Did you find what you wanted?", "kind": "yesno"})
client.post("/crm-feedback/questions/save", data={"text": "How did you hear of us?", "kind": "choice",
                                                  "options": "Instagram\nA friend\nNewspaper"})
r = client.post("/crm-feedback/questions/save", data={"text": "Bad choice", "kind": "choice", "options": "One"})
with app.app_context():
    check("three questions (a one-option choice refused)", FeedbackQuestion.query.count(), 3)
    q1, q2, q3 = FeedbackQuestion.query.order_by(FeedbackQuestion.id).all()
    token = URLSafeSerializer(app.config["SECRET_KEY"], salt="feedback").dumps(PAID)
public = app.test_client()
r = public.get(f"/feedback/{token}")
ok("the customer's page asks the questions", b"How helpful was our staff?" in r.data)
r = public.post(f"/feedback/{token}", data={"rating": "2", f"q{q2.id}": "no"})
ok("a required question left blank is asked again", b"Please answer" in r.data)
r = public.post(f"/feedback/{token}", data={"rating": "2", f"q{q1.id}": "3", f"q{q2.id}": "no",
                                             f"q{q3.id}": "A friend", "comments": "Slow billing"})
ok("thanked", b"Thank you" in r.data)
with app.app_context():
    check("answers saved", FeedbackAnswer.query.count(), 3)
    check("yes/no scored", FeedbackAnswer.query.filter_by(question_id=q2.id).first().score, 0.0)
r = client.get("/crm-feedback/")
ok("dashboard flags it for a call back", b"Slow billing" in r.data)
r = client.get("/crm-feedback/report")
ok("report shows the answers", b"A friend" in r.data)
client.post("/crm-feedback/form", data={"title": "Rate us", "which": "some", "question_ids": [str(q3.id)],
                                        "alert_at": "2", "show_comment": "on"})
r = public.get(f"/feedback/{token}")
ok("one bill takes one answer", b"Thank you" in r.data)
r = client.get("/crm-feedback/form/preview")
ok("the form shows only the chosen question", b"How did you hear" in r.data and b"How helpful" not in r.data
   and b"Rate us" in r.data)
r = client.post(f"/crm-feedback/questions/{q1.id}/delete")
with app.app_context():
    check("an answered question cannot be deleted", db.session.get(FeedbackQuestion, q1.id) is not None, True)

# ---- customer divert ---------------------------------------------------------
print("\n-- a walk-in sent to a salesperson, closed by the bill they bought on")
client.post("/customer-divert/new", data={"customer_phone": "9876500001", "looking_for": "Sarees",
                                          "staff_id": "1"})
with app.app_context():
    v = CustomerVisit.query.first()
    check("recognised as a returning customer", v.customer_id, KUMAR)
    check("attending", v.status, "attending")
    VID = v.id
    inv = Invoice(invoice_number="INV-000304", customer_id=KUMAR, cashier_id=1, total=900)
    db.session.add(inv)
    db.session.commit()
client.post(f"/customer-divert/{VID}/status", data={"status": "billed"})
with app.app_context():
    v = db.session.get(CustomerVisit, VID)
    check("billed, and the bill found by itself", (v.status, v.invoice.invoice_number if v.invoice else None),
          ("billed", "INV-000304"))

# ---- leads -------------------------------------------------------------------
print("\n-- a lead followed up and won becomes a customer")
client.post("/leads/new", data={"name": "Divya Wedding", "phone": "9222222222", "source": "instagram",
                                "interest": "Bridal", "expected_value": "80000"})
with app.app_context():
    lead = Lead.query.first()
    LID = lead.id
client.post(f"/leads/{LID}/activity", data={"kind": "call", "note": "Visiting Saturday", "status": "interested",
                                            "next_follow_up": (today + timedelta(days=3)).isoformat()})
client.post(f"/leads/{LID}/convert")
with app.app_context():
    lead = db.session.get(Lead, LID)
    check("won", lead.status, "won")
    check("a customer now", lead.customer.phone if lead.customer else None, "9222222222")
    check("three entries in its history", len(lead.activities), 3)

# ---- self registration ----------------------------------------------------
print("\n-- the sign-up QR files a customer for approval")
client.post("/registration/", data={"title": "Join us", "f_email": "required", "f_city": "optional",
                                    "f_dob": "off"})
with app.test_request_context():
    from app.routes.crm_people import join_token
    jt = join_token(0)
r = public.post(f"/join/{jt}", data={"name": "Anitha", "phone": "9333333333"})
ok("a required field is asked for", b"Please fill in: Email" in r.data)
r = public.post(f"/join/{jt}", data={"name": "Anitha", "phone": "9333333333", "email": "a@x.in", "city": "Salem"})
ok("welcomed", b"Welcome" in r.data)
with app.app_context():
    a = Customer.query.filter_by(phone="9333333333").first()
    check("waiting for approval", (a.source, a.awaiting_approval, a.city), ("self", True, "Salem"))
    AID = a.id
r = public.post(f"/join/{jt}", data={"name": "Someone", "phone": "9333333333", "email": "b@x.in"})
ok("a second sign-up on the number says so without naming anyone", b"already registered" in r.data
   and b"Anitha" not in r.data)
r = public.get("/join/not-a-real-token")
check("a made-up link opens nothing", r.status_code, 404)
client.post("/registration/review", data={"ids": [str(AID)], "action": "approve"})
with app.app_context():
    check("approved", db.session.get(Customer, AID).awaiting_approval, False)

# ---- CRM reports ------------------------------------------------------------
print("\n-- the CRM's registers are in Reports, and they agree with the screens")
from app import retail_reports                                        # noqa: E402
crm_keys = [r["key"] for g in retail_reports.catalogue() if g["key"] == "crm" for r in g["reports"]]
check("a CRM Reports group with ten registers", len(crm_keys), 10)
span = f"from={(today - timedelta(days=30)).isoformat()}&to={today.isoformat()}"
for key in crm_keys + ["gv_cn_consumption", "cn_customer", "loyalty_consumption"]:
    r = client.get(f"/reports/r/{key}?{span}")
    ok(f"report {key} opens", r.status_code == 200, f"HTTP {r.status_code}")
with app.app_context():
    run = lambda k: retail_reports.run(k, today - timedelta(days=30), today)  # noqa: E731
    gv = run("gv_cn_consumption")
    check("gift vouchers spent at Settlement are in the consumption register",
          sorted(r[8] for r in gv["rows"] if r[3] == "Gift voucher"), [300.0, 700.0])
    bal = run("gift_voucher_balance")
    check("the spent voucher is not on the balance register", CODE in [r[0] for r in bal["rows"]], False)
    check("the two ₹500s and three complimentary ₹100s are", bal["totals"]["Unspent (valid)"], 1300.0)
    check("voucher sales", run("gift_voucher_sales")["totals"]["Received"], 2000.0)
    check("orders", run("crm_orders")["totals"]["Value"], 1050.0)
    check("walk-ins and conversion", (run("walkin_report")["totals"]["Walk-ins"],
                                      run("walkin_report")["totals"]["Bought"]), (1, 1))
    check("leads won", run("leads_report")["totals"]["Won"], 1)
    check("loyalty points turned into a coupon", run("loyalty_ledger")["totals"]["To coupon"], -100.0)
    check("self sign-ups", run("self_registrations")["totals"]["Sign-ups"], 1)
    check("price list customers", run("price_list_customers")["totals"]["Customers"], 1)

# ---- a Store user is kept to their screens --------------------------------
print("\n-- a Store user (billing only) does not reach the CRM's manager screens")
USER = {modules.STORE_HEADER: "user"}
r = client.get("/crm-analyser/", headers=USER)
ok("analyser refused", r.status_code in (302, 403), f"HTTP {r.status_code}")
r = public.get(f"/join/{jt}", headers=USER)
check("the public sign-up page still opens", r.status_code, 200)

# ---- a cashier is kept off the manager screens ----------------------------
login("till")
r = client.get("/price-list/")
check("a cashier cannot open the price list", r.status_code, 403)
r = client.get("/orders/")
check("…but takes orders", r.status_code, 200)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all CRM checks passed")
