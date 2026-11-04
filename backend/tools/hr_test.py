"""HR & Payroll, end to end — a month in the life of a store's people.

    python backend/tools/hr_test.py

Two throwaway databases, as the Command Center test builds them: the warehouse's
(where HR lives) and a till's (where staff sell and check in). A month is run
through every screen's API in the order a store runs it — people joined, days
marked by hand, by file, by device and by the till, leave asked for and
approved, an advance given, overtime approved, incentive earned on the till's
bills, the payroll worked out, approved into the books, paid, and a review cycle
closed with an increment. Each step is checked for the figure it should give.
"""
import calendar
import datetime as dt
import math
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHOP = ROOT / "Textile Retail Shop"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

bad = []


def eq(what, got, want):
    if got != want:
        bad.append(what)
        print("  FAIL  %s\n        got  %r\n        want %r" % (what, got, want))
    else:
        print("  ok    %s" % what)


def has(what, text, wanted):
    ok = wanted.lower() in str(text).lower()
    if not ok:
        bad.append(what)
        print("  FAIL  %s\n        %r\n        does not contain %r" % (what, text, wanted))
    else:
        print("  ok    %s" % what)


def head(t):
    print("\n%s" % t)


tmp = tempfile.mkdtemp(prefix="essa-hr-")
WH_DB = os.path.join(tmp, "warehouse.db").replace(os.sep, "/")
SHOP_DB = os.path.join(tmp, "shop.db").replace(os.sep, "/")
for leak in ("ESSA_DATABASE_URL", "POSTGRES_URL", "POSTGRES_PRISMA_URL", "POSTGRES_URL_NON_POOLING",
             "SHOP_DB_SCHEMA", "ANTHROPIC_API_KEY", "ESSA_SUPERBOSS_PASSWORD", "BLOB_READ_WRITE_TOKEN"):
    os.environ.pop(leak, None)
os.environ["ESSA_STATE_DIR"] = tmp
os.environ["ESSA_POS_DB"] = SHOP_DB
os.environ["ESSA_WAREHOUSE_DB"] = os.path.join(tmp, "no-warehouse-here.db")

OFFSET = dt.timedelta(minutes=int(os.environ.get("ESSA_UTC_OFFSET_MINUTES", "330")))
TODAY = (dt.datetime.utcnow() + OFFSET).date()
# The month the payroll is run for: last month, so every day of it is past.
PM_LAST = TODAY.replace(day=1) - dt.timedelta(days=1)
PM_FIRST = PM_LAST.replace(day=1)
MONTH = PM_FIRST.strftime("%Y-%m")
NDAYS = calendar.monthrange(PM_FIRST.year, PM_FIRST.month)[1]


def pm(d):
    return PM_FIRST.replace(day=d)


def workday(start):
    """The first day of last month from `start` on that is not a Sunday."""
    d = pm(start)
    while d.weekday() == 6:
        d += dt.timedelta(days=1)
    return d


def after(d):
    """The next day after `d` that is not a Sunday."""
    d += dt.timedelta(days=1)
    while d.weekday() == 6:
        d += dt.timedelta(days=1)
    return d


def utc(d, h, m=0):
    return dt.datetime.combine(d, dt.time(h, m)) - OFFSET


# ===========================================================================
head("a till where Priya and Ravi sell, and Priya checks in")
os.environ["DATABASE_URL"] = "sqlite:///" + SHOP_DB
sys.path.insert(0, str(SHOP))
import app as shop_pkg                                              # noqa: E402
from app import db as shop_db                                       # noqa: E402
from app.models import (Attendance as SAttendance, Category, Company, Invoice,  # noqa: E402
                        InvoiceItem, Location, Product, User as SUser)

shop_app = shop_pkg.create_app()
ctx = shop_app.app_context()
ctx.push()
shop_db.create_all()
co = Company(name="TAQUA SILKS", is_default=True, active=True)
priya_s = SUser(username="priya", full_name="Priya Sharma", role="cashier")
priya_s.set_password("x")
ravi_s = SUser(username="ravi", full_name="Ravi Kumar", role="cashier", salary=11000)
ravi_s.set_password("x")
saree_c, kurti_c = Category(name="SAREE"), Category(name="KURTI")
shop_db.session.add_all([co, priya_s, ravi_s, saree_c, kurti_c])
shop_db.session.flush()
branch = Location(name="TAQUA TIRUPUR", company_id=co.id, active=True)
shop_db.session.add(branch)
shop_db.session.flush()
old_saree = Product(sku="SAR-001", barcode="8900001", name="SILK SAREE", category_id=saree_c.id, brand="TAQUA",
                    selling_price=2000, cost_price=1200, purchase_date=PM_FIRST - dt.timedelta(days=200))
new_kurti = Product(sku="KUR-001", barcode="8900002", name="COTTON KURTI", category_id=kurti_c.id, brand="AURA",
                    selling_price=1000, cost_price=600, purchase_date=PM_FIRST)
shop_db.session.add_all([old_saree, new_kurti])
shop_db.session.flush()


def sell(no, who, lines, d):
    total = sum(q * p for _, q, p in lines)
    inv = Invoice(invoice_number=no, cashier_id=who.id, staff_id=who.id, invoice_date=utc(d, 13),
                  subtotal=total, total=total, company_id=co.id, location_id=branch.id,
                  payment_status="paid", payment_method="cash", fin_year="26")
    shop_db.session.add(inv)
    shop_db.session.flush()
    for prod, q, p in lines:
        shop_db.session.add(InvoiceItem(invoice_id=inv.id, product_id=prod.id, quantity=q, unit_price=p,
                                        line_total=q * p, gst_rate=5))


sell("B-001", priya_s, [(old_saree, 2, 2000.0), (new_kurti, 1, 1000.0)], pm(5))
sell("B-002", ravi_s, [(new_kurti, 1, 1000.0)], pm(6))
STORE_DAY = workday(20)
shop_db.session.add(SAttendance(user_id=priya_s.id, check_in=utc(STORE_DAY, 9, 58), check_out=utc(STORE_DAY, 21, 5)))
shop_db.session.commit()
ctx.pop()
for name in [k for k in list(sys.modules) if k == "app" or k.startswith("app.") or k == "config"]:
    del sys.modules[name]
sys.path.remove(str(SHOP))
print("  ok    the till has two bills and a check-in")

# ===========================================================================
head("the warehouse, signed in as the super admin")
os.environ["DATABASE_URL"] = "sqlite:///" + WH_DB
sys.path.insert(0, str(ROOT))
from backend.app import models                                      # noqa: E402
from backend.app.database import SessionLocal, engine               # noqa: E402
from backend.app.main import app                                    # noqa: E402
from backend.app.services import hr as hr_svc, permissions          # noqa: E402
from fastapi.testclient import TestClient                           # noqa: E402

models.Base.metadata.create_all(bind=engine)
client = TestClient(app)
tok = client.post("/api/auth/login", headers={"X-Essa-Client": "native"},
                  json={"username": "superadmin", "password": "super@123"}).json()["token"]
H = {"Authorization": "Bearer " + tok}


def get(path, **params):
    r = client.get("/api/hr/" + path, headers=H, params=params)
    if r.status_code != 200:
        print("        GET", path, r.status_code, r.text[:300])
    return r.json()


def send(method, path, body=None, expect=200):
    r = client.request(method, "/api/hr/" + path, headers=H, json=body)
    if r.status_code != expect:
        print("        %s %s %s %s" % (method, path, r.status_code, r.text[:300]))
    return r


eq("HR is a grant on Users & Access", "hr" in permissions.SCREEN_KEYS, True)
eq("nobody signed in is turned away", TestClient(app).get("/api/hr/options").status_code, 401)
o = get("options")
eq("a first-run set of departments", len(o["departments"]), 6)
eq("…leave types", [t["code"] for t in o["leave_types"]], ["CL", "SL", "EL", "CO", "LOP"])
eq("…and shifts", [s["name"] for s in o["shifts"]], ["General", "Morning", "Evening"])
dep = {d["name"]: d["id"] for d in o["departments"]}
desig = {d["name"]: d["id"] for d in o["designations"]}
general = o["shifts"][0]["id"]

# ===========================================================================
head("employee records — joining, the role history and the hierarchy")
r = send("POST", "employees", {"name": "Kumar Raj", "joined_on": "2020-04-01", "phone": "9876500010", "dob": "14-03-1982",
                               "department_id": dep["Sales"], "designation_id": desig["Store Manager"],
                               "location": "TAQUA TIRUPUR", "shift_id": general, "weekly_off": "Sunday",
                               "salary": {"basic": 30000, "hra": 15000, "special": 15000},
                               "pan": "ABCDE1234F"})
kumar = r.json()
eq("the first employee is EMP0001", kumar["code"], "EMP0001")
eq("a date of birth before 1990 is a date", kumar["dob"], "1982-03-14")
eq("…and joining is on their history", [h["kind"] for h in kumar["history"]], ["Joined"])
r = send("POST", "employees", {"name": "Priya Sharma", "joined_on": "2024-06-10", "phone": "9876500011",
                               "department_id": dep["Billing"], "designation_id": desig["Cashier"],
                               "location": "TAQUA TIRUPUR", "shift_id": general, "weekly_off": "Sunday",
                               "reporting_to_id": kumar["id"], "store_username": "priya",
                               "bank_name": "HDFC", "bank_account": "50100123456", "ifsc": "hdfc0001234",
                               "salary": {"basic": 11000, "hra": 4000, "special": 2000}})
priya = r.json()
eq("IFSC is kept in capitals", priya["ifsc"], "HDFC0001234")
r = send("POST", "employees", {"name": "Arjun Mehta", "joined_on": "2019-01-01", "weekly_off": "Sunday",
                               "department_id": dep["Administration"], "designation_id": desig["General Manager"],
                               "shift_id": general, "salary": {"basic": 75000, "hra": 37500, "special": 37500},
                               "esi_applicable": False})
arjun = r.json()
eq("a PAN that is not one is refused", send("POST", "employees", {"name": "X", "joined_on": "2024-01-01",
                                                                  "pan": "12345"}, 400).status_code, 400)
eq("…and so is a missing joining date", send("POST", "employees", {"name": "Y"}, 400).status_code, 400)
eq("a manager cannot report to their own report",
   send("PUT", f"employees/{kumar['id']}", {"reporting_to_id": priya["id"]}, 400).json()["detail"],
   "That would make them report to someone who reports to them")
r = send("PUT", f"employees/{priya['id']}", {"designation_id": desig["Sales Executive"],
                                             "salary": {"basic": 12000, "hra": 4000, "special": 2000}})
kinds = [h["kind"] for h in r.json()["history"]]
eq("a new designation and a raise go on the role history by themselves",
   sorted(kinds), ["Increment", "Joined", "Re-designation"])
has("…saying what changed", [h["detail"] for h in r.json()["history"] if h["kind"] == "Increment"][0], "₹17,000 → ₹18,000")
send("PUT", f"employees/{priya['id']}", {"designation_id": desig["Cashier"]})
eq("one Store login is one employee",
   send("POST", "employees", {"name": "Z", "joined_on": "2024-01-01", "store_username": "PRIYA"}, 400).status_code, 400)
st = get("store-staff")
eq("the Store's staff are listed with who they are linked to",
   {r["username"]: r["employee"] for r in st["rows"]}, {"priya": "Priya Sharma", "ravi": None})
chart = get("org-chart")
eq("the chart hangs Priya under Kumar",
   {n["name"]: n["manager_id"] for n in chart["nodes"]}[priya["name"]], kumar["id"])
eq("…and counts Kumar's reports", {n["name"]: n["reports"] for n in chart["nodes"]}["Kumar Raj"], 1)
r = send("POST", f"employees/{priya['id']}/contracts", {"contract_type": "Appointment", "start_date": "2024-06-10",
                                                         "end_date": "2024-01-01"}, 400)
eq("a contract that ends before it starts is refused", r.status_code, 400)
send("POST", f"employees/{priya['id']}/contracts", {"contract_type": "Fixed term", "start_date": "2024-06-10",
                                                     "end_date": (TODAY + dt.timedelta(days=20)).isoformat(),
                                                     "ctc_annual": 216000})
r = client.post(f"/api/hr/employees/{priya['id']}/documents", headers=H,
                data={"doc_type": "Aadhaar", "number": "1234 5678 9012",
                      "expires_on": (TODAY + dt.timedelta(days=10)).isoformat()},
                files={"file": ("aadhaar.pdf", b"%PDF-1.4 test", "application/pdf")})
eq("a document is uploaded", r.status_code, 200)
doc = r.json()
eq("…and is flagged as expiring soon", doc["expiring"], True)
eq("…and its file comes back", client.get(f"/api/hr/employees/{priya['id']}/documents/{doc['id']}/file",
                                          headers=H).content, b"%PDF-1.4 test")
eq("…filed in its folder", doc["group"], "ID proofs")
r = client.post(f"/api/hr/employees/{priya['id']}/documents", headers=H, data={"doc_type": "Resignation letter"},
                files={"file": ("resign.pdf", b"%PDF-1.4 r", "application/pdf")})
eq("a resignation letter has a kind and a folder of its own", r.json().get("group"), "Conduct & exit")
eq("every folder's kinds are offered", [g for g, _ in get("options")["doc_groups"]],
   ["ID proofs", "Joining", "Pay letters", "Education & certificates", "Previous employment", "Conduct & exit", "Other"])

head("the employee's photo")
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
r = client.post(f"/api/hr/employees/{priya['id']}/photo", headers=H, files={"file": ("me.pdf", b"%PDF", "application/pdf")})
eq("a PDF is not a photo", r.status_code, 400)
r = client.post(f"/api/hr/employees/{priya['id']}/photo", headers=H, files={"file": ("me.png", PNG, "image/png")})
v1 = r.json()["photo"]
eq("a picture becomes their photo", bool(v1), True)
eq("…served back for the banner", client.get(f"/api/hr/employees/{priya['id']}/photo", headers=H).content, PNG)
eq("…and kept in their file", any(d["doc_type"] == "Photo" for d in r.json()["documents"]), True)
r = client.post(f"/api/hr/employees/{priya['id']}/photo", headers=H,
                files={"file": ("me2.png", PNG + b"\x00", "image/png")})
eq("a new picture changes the version, so no browser shows the old one", r.json()["photo"] != v1, True)
r = client.delete(f"/api/hr/employees/{priya['id']}/photo", headers=H)
eq("removing it takes it off the record", r.json()["photo"], None)
eq("…but not out of the file", sum(d["doc_type"] == "Photo" for d in r.json()["documents"]), 2)

# ===========================================================================
head("attendance — by hand, from a file, from a device and from the till")
LATE_DAY, OT_DAY, ABSENT_DAY = workday(3), workday(8), workday(12)
r = send("PUT", f"attendance/{priya['id']}/{LATE_DAY.isoformat()}", {"in_time": "10:20", "out_time": "21:30"})
eq("in at 10:20 on a 10:00 shift is 20 minutes late", r.json()["late"], 20)
eq("…a full day, no overtime", (r.json()["status"], r.json()["ot"]), ("P", 0))
r = send("PUT", f"attendance/{priya['id']}/{OT_DAY.isoformat()}", {"in_time": "10:00", "out_time": "23:00"})
eq("staying till 23:00 is two hours over", r.json()["ot"], 120)
send("PUT", f"attendance/{priya['id']}/{ABSENT_DAY.isoformat()}", {"status": "A"})
FILE_DAY = workday(15)
csv_text = ("Emp Code,Punch Time\n"
            f"EMP0002,{FILE_DAY.strftime('%d-%m-%Y')} 10:02\n"
            f"EMP0002,{FILE_DAY.strftime('%d-%m-%Y')} 21:01\n"
            f"EMP9999,{FILE_DAY.strftime('%d-%m-%Y')} 10:00\n")
r = client.post("/api/hr/attendance/import", headers=H, files={"file": ("bio.csv", csv_text.encode(), "text/csv")})
eq("a biometric export of punches is read", r.json()["days"], 1)
eq("…and a code nobody has is reported, not guessed", [u["key"] for u in r.json()["unknown"]], ["EMP9999"])
DEV_DAY = workday(17)
key = get("settings")["device_key"]
punch = {"punches": [{"id": "priya", "time": f"{DEV_DAY.isoformat()} 09:55"},
                     {"id": "priya", "time": f"{DEV_DAY.isoformat()} 20:59"}]}
eq("a device without the key is turned away",
   client.post("/api/hr/device/punches", json=punch, headers={"X-Attendance-Key": "nope"}).status_code, 401)
r = client.post("/api/hr/device/punches", json=punch, headers={"X-Attendance-Key": key})
eq("…and with it, its punches are a day's attendance", r.json().get("days"), 1)
r = send("POST", "attendance/sync-store", {"date_from": PM_FIRST.isoformat(), "date_to": PM_LAST.isoformat()})
eq("the till's check-in is a day too", r.json().get("days"), 1)
reg = get("attendance", month=MONTH)
prow = next(x for x in reg["rows"] if x["id"] == priya["id"])
eq("the register shows where each day came from",
   [prow["days"][d.isoformat()]["src"] for d in (LATE_DAY, FILE_DAY, DEV_DAY, STORE_DAY)],
   ["manual", "import", "device", "store"])
first_sunday = next(d for d in (pm(i) for i in range(1, 8)) if d.weekday() == 6)
eq("a Sunday nobody marked is her weekly off", prow["days"][first_sunday.isoformat()]["s"], "WO")
ot = get("overtime", date_from=PM_FIRST.isoformat(), date_to=PM_LAST.isoformat())
eq("the overtime waits for approval", [(x["name"], x["ot_minutes"], x["status"]) for x in ot],
   [("Priya Sharma", 120, "pending")])
send("POST", "overtime/decide", {"ids": [ot[0]["id"]], "decision": "approved"})
r = send("POST", "roster", {"employee_ids": [priya["id"]], "date_from": pm(1).isoformat(), "shift_id": None}, 400)
eq("a roster change needs a shift or a day off", r.status_code, 400)

# ===========================================================================
head("leave — accrual, applying, approval and the balance")
r = send("POST", "leave-accrual", {"month": MONTH})
eq("the month's leave is credited", r.json()["credited"] > 0, True)
again = send("POST", "leave-accrual", {"month": MONTH}).json()["credited"]
eq("…and only once", again, 0)
bal = {b["code"]: b["balance"] for b in get("leave-balances", employee_id=priya["id"])}
eq("a month of Casual Leave is one day", bal["CL"], 1.0)
eq("Sick Leave is the year's quota", bal["SL"], 12.0)
CL_FROM = workday(21)
CL_TO = after(CL_FROM)
r = send("POST", "leave", {"employee_id": priya["id"], "leave_type_id": o["leave_types"][0]["id"],
                           "from_date": CL_FROM.isoformat(), "to_date": CL_TO.isoformat()}, 400)
has("two days of CL with one in hand is refused, saying so", r.json()["detail"], "1 day(s) of CL left")
SL_DAY = after(CL_TO)
r = send("POST", "leave", {"employee_id": priya["id"], "leave_type_id": o["leave_types"][1]["id"],
                           "from_date": SL_DAY.isoformat(), "reason": "fever"})
lv = r.json()
eq("a day's sick leave is asked for", (lv["status"], lv["days"]), ("pending", 1.0))
send("POST", f"leave/{lv['id']}/approved", {"note": "get well"})
LOP_DAY = after(SL_DAY)
send("POST", "leave", {"employee_id": priya["id"], "leave_type_id": o["leave_types"][4]["id"],
                       "from_date": LOP_DAY.isoformat(), "approve": True})
bal = {b["code"]: b["balance"] for b in get("leave-balances", employee_id=priya["id"])}
eq("approving takes it off the balance", bal["SL"], 11.0)
reg = get("attendance", month=MONTH)
prow = next(x for x in reg["rows"] if x["id"] == priya["id"])
eq("…and marks the days as leave", (prow["days"][SL_DAY.isoformat()]["s"], prow["days"][LOP_DAY.isoformat()]["s"]),
   ("L", "L"))
eq("the month's tally: one absence and one unpaid leave are loss of pay", prow["totals"]["lop"], 2.0)
r = send("POST", "leave", {"employee_id": priya["id"], "leave_type_id": o["leave_types"][1]["id"],
                           "from_date": SL_DAY.isoformat()}, 400)
has("the same day twice is refused", r.json()["detail"], "already has")

# ===========================================================================
head("an advance, given and recovered from salary")
r = send("POST", "advances", {"employee_id": priya["id"], "amount": 3000, "emi": 1000,
                              "issued_on": pm(2).isoformat(), "recover_from": MONTH, "reason": "festival"})
adv = r.json()
eq("an advance of ₹3,000 at ₹1,000 a month", (adv["amount"], adv["emi"], adv["balance"]), (3000.0, 1000.0, 3000.0))
db = SessionLocal()
v = db.get(models.Voucher, adv["voucher_id"])
eq("…is a Payment in the books", (v.vtype, v.amount, v.source), ("Payment", 3000.0, "hr_advance"))
db.close()

# ===========================================================================
head("sales incentive — by value, and by piece on old stock")
send("POST", "incentive-schemes", {"name": "1% on everything", "basis": "pct", "rate": 1})
send("POST", "incentive-schemes", {"name": "₹100 a piece on stock over 90 days", "basis": "per_piece", "rate": 100,
                                   "filters": {"age_min": 90}})
send("POST", "incentive-schemes", {"name": "Brand push", "basis": "pct", "rate": 5,
                                   "filters": {"brands": "AURA", "barcodes": "8900002"}})
eq("a scheme with no rate is refused", send("POST", "incentive-schemes", {"name": "x", "basis": "pct"}, 400).status_code, 400)
inc = get("incentives", date_from=PM_FIRST.isoformat(), date_to=PM_LAST.isoformat())
row = next(x for x in inc["rows"] if x["name"] == "Priya Sharma")
eq("Priya sold ₹5,000 on one bill", (row["sales"], row["bills"]), (5000.0, 1))
eq("1% of it, ₹100 a piece on the two old sarees, 5% on the AURA kurti",
   sorted(row["by_scheme"].values()), [50.0, 50.0, 200.0])
eq("…₹300 in all", row["incentive"], 300.0)
eq("Ravi's sales are shown as not linked to an employee", [u["username"] for u in inc["unlinked"]], ["ravi"])

# ===========================================================================
head("payroll — worked out, adjusted, approved into the books, paid")
r = send("POST", f"payroll/{MONTH}/compute")
run = r.json()
slip = {s["name"]: s for s in run["slips"]}
p = slip["Priya Sharma"]
eq("everyone with a salary is on it", sorted(slip), ["Arjun Mehta", "Kumar Raj", "Priya Sharma"])
eq("Priya is paid for the month less two days' loss of pay", (p["lop_days"], p["paid_days"]), (2.0, NDAYS - 2.0))
factor = (NDAYS - 2) / NDAYS
eq("basic is pro-rated", p["earnings"]["basic"], float(math.floor(12000 * factor + 0.5)))
eq("the incentive is on the payslip", p["earnings"]["incentive"], 300.0)
ot_rate = 18000 / 30 / 10 * 2
eq("two approved hours of overtime at twice the hourly rate", p["earnings"]["ot"], float(math.floor(ot_rate * 2 + 0.5)))
eq("PF is 12% of basic", p["deductions"]["pf"], float(math.floor(p["earnings"]["basic"] * 0.12 + 0.5)))
eq("ESI is 0.75% of gross, rounded up", p["deductions"]["esi"], float(math.ceil(p["gross"] * 0.0075)))
eq("the advance instalment is taken", p["deductions"]["advance"], 1000.0)
eq("net is gross less deductions", p["net"], round(p["gross"] - p["total_deductions"], 2))
k = slip["Kumar Raj"]
eq("no ESI over the wage limit", "esi" in k["deductions"], False)
eq("…and PF on basic only up to the ₹15,000 ceiling", k["deductions"]["pf"], 1800.0)
eq("no TDS on ₹7.2 lakh a year under the new regime", "tds" in k["deductions"], False)
a = slip["Arjun Mehta"]
eq("TDS on ₹18 lakh a year: ₹1,45,000 plus cess, a twelfth a month", a["deductions"]["tds"], 12567.0)
r = send("PUT", f"payslips/{p['id']}", {"bonus": 500, "remarks": "Diwali"})
eq("a bonus added by hand is on the payslip", r.json()["earnings"]["bonus"], 500.0)
run = send("POST", f"payroll/{MONTH}/compute").json()
eq("…and survives working the month out again",
   next(s for s in run["slips"] if s["name"] == "Priya Sharma")["earnings"]["bonus"], 500.0)
r = send("POST", f"payroll/{MONTH}/approve")
run = r.json()
eq("approving posts a Journal to the books", (run["status"], run["journal"]["voucher_no"][:3]), ("approved", "JRN"))
db = SessionLocal()
jv = db.get(models.Voucher, run["journal"]["id"])
dr = round(sum(l.debit for l in jv.lines), 2)
cr = round(sum(l.credit for l in jv.lines), 2)
eq("…balanced", dr, cr)
t = run["totals"]
eq("…for gross plus the employer's PF and ESI", dr, round(t["gross"] + t["employer_pf"] + t["employer_esi"], 2))
names = {l.ledger.name for l in jv.lines}
eq("…to the salary ledgers", {"Salaries & Wages", "Salary Payable", "PF Payable", "ESI Payable", "TDS Payable",
                              "Professional Tax Payable", "Staff Advances"} <= names, True)
db.close()
eq("the advance is down by the instalment", get("advances")[0]["balance"], 2000.0)
r = send("POST", "leave", {"employee_id": kumar["id"], "leave_type_id": o["leave_types"][4]["id"],
                           "from_date": after(LOP_DAY).isoformat(), "approve": True}, 400)
has("leave cannot be changed in an approved month", r.json()["detail"], "reopen it")
r = send("POST", f"payroll/{MONTH}/reopen")
eq("reopening puts it back to draft", r.json()["status"], "draft")
db = SessionLocal()
eq("…cancels the journal", db.get(models.Voucher, jv.id).status, "cancelled")
db.close()
eq("…and gives the instalment back", get("advances")[0]["balance"], 3000.0)
send("POST", f"payroll/{MONTH}/approve")
banks = get("options")["banks"]
r = send("POST", f"payroll/{MONTH}/pay", {"date": TODAY.isoformat(), "mode": "NEFT"})
run = r.json()
eq("paying posts the Payment", (run["status"], run["payment"]["voucher_no"][:3]), ("paid", "PAY"))
eq("…for the net of every payslip", run["payment"]["amount"], run["totals"]["net"])
eq("a paid month cannot be reopened", send("POST", f"payroll/{MONTH}/reopen", expect=400).status_code, 400)
bank = client.get(f"/api/hr/payroll/{MONTH}/bank-file", headers=H).text
has("the bank file carries her account", bank, "50100123456")
ps = get(f"payslips/{p['id']}")
eq("the payslip knows the year so far", ps["ytd"]["months"], 1)
eq("…and who it is for", (ps["snapshot"]["name"], ps["snapshot"]["ifsc"]), ("Priya Sharma", "HDFC0001234"))

# ===========================================================================
head("performance — goals, KPIs from the till, and an appraisal applied")
cyc = send("POST", "cycles", {"name": "Last month review", "period_from": PM_FIRST.isoformat(),
                              "period_to": PM_LAST.isoformat()}).json()
send("POST", "goals", {"cycle_id": cyc["id"], "employee_id": priya["id"], "title": "Sales", "metric": "sales",
                       "weight": 60, "target": 4000})
send("POST", "goals", {"cycle_id": cyc["id"], "employee_id": priya["id"], "title": "Customer feedback",
                       "weight": 40, "target": 10, "actual": 8})
rv = get(f"cycles/{cyc['id']}/review", employee_id=priya["id"])
row = rv["rows"][0]
eq("the sales KPI reads her sales from the till", row["goals"][0]["actual"], 5000.0)
eq("weighted score: 60% at 125 and 40% at 80", row["score"], 107.0)
eq("…suggests a 4", row["suggested"], 4)
ap = send("POST", "appraisals", {"cycle_id": cyc["id"], "employee_id": priya["id"], "rating": 4,
                                 "increment_pct": 10, "new_designation_id": desig["Floor Supervisor"],
                                 "effective_on": TODAY.isoformat(), "status": "approved"}).json()
send("POST", f"appraisals/{ap['id']}/apply")
e = get(f"employees/{priya['id']}")
eq("the increment is on every pay component", e["salary"]["basic"], 13200.0)
eq("…the promotion on the designation", e["designation"], "Floor Supervisor")
eq("…and both on the role history", "Appraisal" in [h["kind"] for h in e["history"]], True)

# ===========================================================================
head("leaving — notice, exit and who people report to after")
r = send("POST", f"employees/{kumar['id']}/exit", {"last_working_day": (TODAY - dt.timedelta(days=1)).isoformat(),
                                                    "exit_type": "Resigned", "exit_reason": "relocating"})
eq("a last day in the past is an exit", r.json()["status"], "exited")
eq("…and his report now reports to whoever he did", get(f"employees/{priya['id']}")["reporting_to_id"], None)
eq("exited people leave the current list", sorted(x["name"] for x in get("employees")),
   ["Arjun Mehta", "Priya Sharma"])
eq("…but are kept", "Kumar Raj" in [x["name"] for x in get("employees", status="exited")], True)

# ===========================================================================
head("the Command Center answers HR questions — for those who may open HR")


def ask(q, headers=H):
    return client.post("/api/command/ask", headers=headers, json={"q": q}).json()


a = ask("Last month's payroll")
eq("routed to payroll", a["intent"], "payroll")
has("…the month, its state and the net", a["line"], f"({run['status']})")
eq("…with the figure the run has", a["headline"].replace(",", "").replace("₹", "").strip().split(".")[0],
   str(int(run["totals"]["net"])))
a = ask("Who is absent today?")
eq("routed to attendance", a["intent"], "attendance")
has("…with the day's tally", a["line"], "present")
a = ask("Who is on leave today?")
eq("routed to staff leave", a["intent"], "staff_leave")
a = ask("Headcount")
eq("routed to headcount", (a["intent"], a["headline"]), ("headcount", "2"))
a = ask("Arjun Mehta")
eq("a name that is an employee answers with their record", a["intent"], "employee")
has("…their designation and pay", a["line"], "General Manager")
r = client.post("/api/users", headers=H, json={"username": "noh", "password": "noh@1234", "role": "admin",
                                               "full_name": "No HR"})
client.put("/api/users/%d/permissions" % r.json()["id"], headers=H,
           json={"screens": {"ask": ["view"], "central": ["view"]}})
tok2 = client.post("/api/auth/login", headers={"X-Essa-Client": "native"},
                   json={"username": "noh", "password": "noh@1234"}).json()["token"]
H2 = {"Authorization": "Bearer " + tok2}
a = ask("Last month's payroll", H2)
eq("an admin not given HR is told so, not shown salaries", (a["ok"], a["intent"]), (False, "payroll"))
a = ask("Arjun Mehta", H2)
eq("…and a name does not open an employee's record for them", a.get("intent") == "employee", False)
eq("…nor can they call HR directly", client.get("/api/hr/employees", headers=H2).status_code, 403)

# ===========================================================================
head("the dashboard and every report")
dash = get("dashboard")
eq("headcount", dash["headcount"]["current"], 2)
eq("the expiring document and contract are flagged", (len(dash["documents"]), len(dash["contracts"])), (1, 1))
eq("last month's payroll is on the trend", dash["payroll"]["trend"][-1]["month"], MONTH)
for k, label, group, params in hr_svc.REPORTS:
    r = client.get(f"/api/hr/reports/{k}", headers=H, params={"month": MONTH, "date_from": PM_FIRST.isoformat(),
                                                             "date_to": TODAY.isoformat()})
    if r.status_code != 200 or "columns" not in r.json():
        eq(f"report {label}", r.status_code, 200)
eq("every report answers", True, True)
pf = client.get("/api/hr/reports/pf_statement", headers=H, params={"month": MONTH}).json()
eq("the PF statement lists everyone on PF", len(pf["rows"]), 3)
reg = client.get("/api/hr/reports/payroll_register", headers=H, params={"month": MONTH}).json()
eq("the payroll register adds up to the run", reg["totals"]["Net"], run["totals"]["net"])

print("\n%d FAILED" % len(bad) if bad else "\nall passing")
sys.exit(1 if bad else 0)
