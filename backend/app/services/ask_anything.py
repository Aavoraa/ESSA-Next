"""Ask anything about the business — and get ONE LINE back first.

    "Today's total sales"
    → Today's Sales: ₹8,42,650 across 4 warehouses, 12 stores and 18 POS counters.

The Reports screen's question box (services/nlq) picks one of forty registers
and hands back the table. That is the right answer for somebody about to work
through a register; it is the wrong one for the owner, who asked a question and
wants the number. So this answers first — a sentence with the figure in it, said
the way the business says money — and keeps the rows behind it for "View
details" and "Export".

HOW A QUESTION IS READ
----------------------
The same way nlq does, for the same reasons: the model's job is narrow and
checkable. It picks one INTENT from a fixed list and fills in the period, the
place and the thing asked about. It never writes a query. Every figure comes from
a function the dashboards already use, so the answer here and the tile on the
Command Center cannot disagree.

Without an API key the rules in `_offline` read the question instead — worse,
and the answer says it matched on keywords.

WHAT AN ACCOUNT IS SHOWN
------------------------
Everything is answered inside the warehouses the account is allotted
(Users & Access). A named warehouse the account is not allotted is refused, not
quietly widened. Product-level figures — dead stock, the stores' low stock — are
company-wide by nature, and say so.

A CODE IS NOT A QUESTION
------------------------
"GRN-2026-00045", "TG26-001245", a QR payload — anything that looks like a
document number or a tag is traced end to end (services/trace) rather than read.
"""
import difflib
import json
import re

from datetime import timedelta

from sqlalchemy import func

from .. import models, runtime
from . import (audit as audit_svc, business_day, command_center as cc, crm_insights as crm, dates as date_svc,
               hr as hr_svc, nlq, pos_insights, stock_locations as stock_loc, trace as trace_svc)
from .figures import inr, inr_short, plural, qty as fmt_qty, spoken_inr

#: What each intent answers — the model reads these lines.
INTENTS = {
    "sales": "Sales taken at the stores' tills (POS) in a period. Optionally at one warehouse's stores, one store, a floor or a counter, of a category/product such as 'ladies shirts', by one staff member ('how much did Priya sell') or to one customer.",
    "sales_rank": "WHICH warehouse, store, floor, counter, cashier, category, product or payment method sold the MOST (or the least) in a period — and sales listed by one of those ('product wise sales yesterday', 'floor wise sales this month').",
    "compare": "A COMPARISON: sales against purchases ('sales vs purchase this month', 'floor wise sales vs purchase', 'product wise sales vs purchase'), or sales in one period against another ('compare sales this month with last month', 'floor wise sales this week vs last week'). group_by says floor/product/category/store when it is asked 'wise'.",
    "bills": "The store BILLS themselves, listed — how many and what they came to in a period, optionally billed by one cashier, sold by one salesperson or bought by one customer. 'Bills billed by Priya today', 'Show R Kumar's bills this month'. (Supplier invoices are `invoices`.)",
    "discounts": "Discounts given on store bills in a period — the total, and who (which cashier) gave the most.",
    "profit": "Profit / gross margin on store sales in a period.",
    "purchases": "Goods received through GRN in a period — how many GRNs, how many units and products, and their value. 'How many products were received through GRN today'.",
    "invoices": "Supplier invoices entered in a period — how many came in, how many are still waiting for review, how many were confirmed and posted. 'Today's total invoices'. (Customer bills at the tills are `sales`.)",
    "lr": "LR / transport entries in a period — consignments booked into the register: how many, how many pieces, their value, and how many have not been received yet. 'Today's total LR entries'.",
    "payments": "Money paid to suppliers in a period.",
    "pending_payments": "Supplier bills still unpaid — how much is outstanding and to whom. 'Pending supplier payments'.",
    "pending_grns": "GRNs not yet posted and supplier invoices still waiting for review.",
    "returns": "Returns in a period — customer returns at the stores and debit notes to suppliers.",
    "stock": "Stock available right now — units and value. Optionally in one warehouse, on a store floor such as 'Ground Floor', or of a category/product.",
    "stock_rank": "WHICH warehouse (or category) holds the most — or least — stock value.",
    "low_stock": "Products at or below their reorder level in the stores — what is running low.",
    "dead_stock": "Dead stock: products with no sale or dispatch for N days (default 90) — how many and what they are worth. 'Products with no sales for 90 days'.",
    "transfers": "Stock transfers and dispatches — what is in transit between buildings, what was sent in a period.",
    "counts": "How many warehouses, stores, POS counters or users there are.",
    "activity": "What people did — actions on the audit trail in a period, optionally by one person. 'What did Ravi do today'.",
    "trace": "Track ONE item or document end to end: a product QR, SKU, piece code, GRN number, supplier invoice number, transfer code, LR number or store bill number.",
    # ---- the CRM (the Store's customer side) ----
    "customer": "ONE customer: their details, loyalty points, spend and recent bills — asked by mobile number, customer code, loyalty card or name. 'Details of 9876543210', 'How many points does Priya have'.",
    "customers": "The customer book: how many customers, new sign-ups in a period, sign-ups awaiting approval, who bought and how many came back. 'How many new customers this month', 'top customers'.",
    "birthdays": "Customers whose birthday or wedding anniversary falls in a period (default the next 7 days).",
    "customer_orders": "CUSTOMER orders (things ordered for later delivery, not supplier purchase orders): taken in a period, still open, due or overdue for delivery, ready for pickup, money paid against them.",
    "gift_vouchers": "Gift vouchers: sold in a period (count, face value, money received), redeemed at the tills, and the unspent balance still out with customers.",
    "coupons": "Gift coupons: issued in a period, redeemed, and the discount they gave — by campaign.",
    "loyalty": "Loyalty points across all customers: earned and redeemed in a period, points still held and what they are worth. (One customer's points is `customer`.)",
    "feedback": "Customer feedback and ratings: responses in a period, average stars, unhappy customers to call back, scores per feedback question.",
    "leads": "Sales leads / enquiries: open leads, follow-ups due today or overdue, new leads, won and lost, pipeline value.",
    "walkins": "Walk-ins / footfall at the door (Customer Divert): how many came in, how many bought, who served them.",
    # ---- HR (only for an account that may open HR) ----
    "payroll": "The salary payroll for a month: gross, net paid, PF, ESI, TDS, cost to company, and whether it is approved or paid. 'Last month's payroll', 'How much salary did we pay in October'.",
    "attendance": "Staff attendance on a day (default today): who is present, absent, late, on leave or not marked yet. 'Who is absent today', 'Today's attendance'.",
    "staff_leave": "Staff leave: who is on leave on a day or in a period, and leave requests waiting for approval. 'Who is on leave today', 'Pending leave requests'.",
    "headcount": "How many people work here — by department and location — and who joined or left in a period. 'Headcount', 'How many employees joined this month'.",
}
#: The intents that read people's pay and personal records.
HR_INTENTS = ("payroll", "attendance", "staff_leave", "headcount")
NONE = "none"
GROUPS = ("warehouse", "store", "floor", "counter", "cashier", "staff", "category",
          "product", "payment", "day")
#: "floor wise", "product-wise", "day wise" — the grouping a list is asked for in.
_WISE = re.compile(r"\b(floor|product|item|category|section|store|branch|shop|counter|cashier|staff|"
                   r"salesman|salesperson|payment|day|date)[ -]?wise\b")
_WISE_GROUP = {"item": "product", "section": "category", "branch": "store", "shop": "store",
               "salesman": "staff", "salesperson": "staff", "date": "day"}
_COMPARE = re.compile(r"\b(?:vs\.?|versus|compared?|comparison|comparing|against)\b|ஒப்பிடு")
#: Who a named person is in a question about bills: the one who billed it, the
#: one who sold it on the floor, the one who bought it — or "" for either of the
#: first two.
ROLES = ("cashier", "salesperson", "customer")


# ---------------------------------------------------------------------------
#  Reading the question — offline
# ---------------------------------------------------------------------------
_RANK = (" which ", " who ", " top ", "highest", " most ", " best ", "biggest", "largest",
         "lowest", " least ", "worst", "maximum", "minimum", "slowest")
_BOTTOM = ("lowest", " least ", "worst", "minimum", "slowest")
_GROUP_WORDS = (
    ("floor", (" floor",)),
    ("counter", (" counter", " till", " pos ")),
    ("warehouse", (" warehouse", " godown")),
    ("store", (" store", " shop", " branch", " outlet")),
    ("cashier", (" cashier", " biller", " staff", " salesman", " salesperson", " employee", " who ")),
    ("category", (" category", " categories", " section")),
    ("product", (" product", " item", " design", " garment")),
    ("payment", ("payment mode", "payment method", " upi", " card ", " tender")),
)
_SALES = (" sale", " sold", " sell", "revenue", "takings", "turnover", "billing",
          "விற்பனை", "விற்ற")
_STOCK = (" stock", "inventory", " on hand", " available", "holding", "இருப்பு", "சரக்கு")
_CODE_TOKEN = re.compile(r"^(?=.*\d)(?=.*[A-Za-z-])[A-Za-z0-9][A-Za-z0-9/_.:-]{3,}$|^\d{6,}$")


def _code_in(question):
    """A document number or tag in the question, or None."""
    q = question.strip()
    toks = q.split()
    if len(toks) == 1 and _CODE_TOKEN.match(q.strip("?.!,")):
        return q.strip("?.!,")
    if re.search(r"\b(track|trace|history of|journey of|where is|follow|locate|find)\b", q, re.I):
        for t in toks:
            t = t.strip("?.!,\"'“”")
            if _CODE_TOKEN.match(t) and not re.match(r"^\d{1,4}$", t):
                return t
    return None


def _has(t, words):
    return any(w in t for w in words)


def _norm(s):
    return " ".join(str(s or "").lower().split())


def _names_in(text, rows, key="name", extra=None):
    """Rows whose name (or code) appears in the text, longest name first."""
    t = f" {_norm(text)} "
    hits = []
    for r in rows:
        for cand in [r.get(key)] + ([r.get(extra)] if extra else []):
            n = _norm(cand)
            if n and len(n) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", t):
                hits.append((len(n), r))
                break
    hits.sort(key=lambda x: -x[0])
    return [r for _, r in hits]


def _warehouse_rows(db):
    return [{"id": w.id, "name": w.name, "code": w.code} for w in db.query(models.Warehouse).all()]


def _store_rows(db):
    return [{"id": s.id, "name": s.name, "code": s.code, "warehouse_id": s.warehouse_id}
            for s in db.query(models.Store).all()]


#: Intents a named person narrows — to the bills they billed, sold or bought.
_PERSONAL = ("sales", "sales_rank", "bills", "profit")
_NOT_A_NAME = {
    "today", "yesterday", "the", "a", "an", "each", "every", "all", "floor", "floors", "counter",
    "counters", "store", "stores", "shop", "branch", "cashier", "cashiers", "staff", "category",
    "categories", "product", "products", "payment", "method", "mode", "warehouse", "warehouses",
    "customer", "customers", "them", "whom", "who", "me", "us", "we", "you", "they", "value",
    "amount", "people", "someone", "anyone", "everyone", "this", "last", "month", "week", "year",
    "day", "date", "time", "hour", "bill", "bills", "salesperson", "salesman", "employee",
    "employees", "name", "number", "upi", "cash", "card", "item", "items", "section", "him", "her",
    "it", "much", "many", "total", "sales", "sale", "how", "what", "which", "show", "list",
    "give", "get", "find", "see", "check", "display", "tell", "about", "of", "for", "most",
    "least", "highest", "lowest", "top", "best", "worst", "more", "less", "billed", "sold",
    "has", "have", "had", "is", "was", "did", "do", "does", "made", "gave", "given", "took"}
_NAME = r"([a-z][a-z.']*(?:\s+[a-z][a-z.']*){0,3}?)"
_NAME_END = (r"(?=\s+(?:today|yesterday|this|last|in|on|at|for|from|between|since|during|till|until|"
             r"sell|sold|sells|bill|billed|bills|have|has|had|do|did|so|and|with|of|by|to)\b"
             r"|\s*[?.!,]|\s*$)")
#: How a question says who — most particular first.
_PERSON_SAID = (
    (r"\b(?:sold|served|attended|handled)\s+by\s+", "salesperson"),
    (r"\b(?:salesperson|salesman|saleswoman|salesgirl)\s+", "salesperson"),
    (r"\b(?:sold|billed|invoiced|sales?|bills?)\s+to\s+", "customer"),
    (r"\bbought\s+by\s+", "customer"),
    (r"\bcustomer\s+", "customer"),
    (r"\b(?:bills?|invoices?|purchases?|shopping|spend(?:ing)?)\s+(?:of|for)\s+", "customer"),
    (r"\b(?:billed|invoiced|raised|cut)\s+by\s+", "cashier"),
    (r"\bcashier\s+", "cashier"),
    (r"\bby\s+", ""),
    (r"\b(?:did|has|have|had)\s+", ""),
    (r"\b(?:sales?|business|billing)\s+(?:of|for)\s+", ""),
)


def _resolve(name, role):
    """Who a name is at the tills: ("staff", [people], role), ("customer",
    [customers], "customer") or None. A role said decides; with none, a name that
    is somebody's WHOLE name wins over one that only starts somebody's — "R Kumar"
    the customer, not Ravi Kumar at the counter — and staff win a tie."""
    staff = pos_insights.people_named(name) if role != "customer" else []
    one, many = None, []
    if role in ("", "customer") and crm.available():
        one = crm.find_customer(name)
        many = [] if one else crm.find_customers_named(name, limit=20)
    whole = _norm(name)
    staff_exact = [p for p in staff if whole in (_norm(p["name"]), _norm(p["username"]))]
    if staff_exact or (staff and not one):
        return "staff", staff_exact or staff, role
    if one or many:
        return "customer", [one] if one else many, "customer"
    return None


def _person_kind(name, role):
    """The role a name really has at the tills, or None when nobody there has it."""
    hit = _resolve(name, role)
    return hit[2] if hit else None


def _person_in(t):
    """(name, role) of the person a question about bills or sales is about — only
    when somebody at the tills is called that. "Sales by floor" names nobody."""
    if not pos_insights.available():
        return "", ""
    said = [(m.group(1), role) for pat, role in _PERSON_SAID
            for m in re.finditer(pat + _NAME + _NAME_END, t)]
    said += [(m.group(1), "") for m in re.finditer(
        r"\b((?:[a-z]\.?\s+)?[a-z][a-z.]+(?:\s+[a-z][a-z.]+)?)'s\s+(?:bills?|sales|billing|invoices?)\b", t)]
    unknown = None
    for name, role in said:
        words = name.strip(" .'").split()
        while words and words[-1] in _NOT_A_NAME:
            words.pop()
        while words and words[0] in _NOT_A_NAME:
            words.pop(0)
        if not words:
            continue
        name = " ".join(words)
        kind = _person_kind(name, role)
        if kind is not None:
            return name, kind
        if role and unknown is None:
            unknown = (name, role)
    # "Billed by Xyz" names a person even when nobody is called that — kept, so
    # the answer says nobody is, rather than quietly answering for everyone.
    return unknown or ("", "")


def _offline(db, question):
    t = f" {question.lower().strip()} "
    today = business_day.today()
    lo, hi = nlq.relative_range(t, today=today)
    m_days = re.search(r"(\d{1,4})\s*\+?\s*days?", t)
    rank = _has(t, _RANK)
    group = next((g for g, words in _GROUP_WORDS if _has(t, words)), "")
    wise = _WISE.search(t)
    if wise:
        group = _WISE_GROUP.get(wise.group(1), wise.group(1))
    code = _code_in(question)

    crm_code = _crm_code(question)
    if crm_code:
        intent, code = "customer" if crm_code[0] == "customer" else "trace", crm_code[1]
    elif code:
        intent = "trace"
    elif re.search(r"\b(payroll|salary|salaries|wages|payslips?|pf|esi|ctc)\b|சம்பளம்", t) \
            and not _has(t, ("supplier", "vendor")):
        intent = "payroll"
    elif re.search(r"\b(attendance|absent|absentees|came late|late today|punched|check[ -]?ins?)\b|வருகை", t) \
            or re.search(r"\bwho (?:is|are|was|were) (?:present|late)\b", t):
        intent = "attendance"
    elif re.search(r"\bon leave\b|\bleave (?:requests?|applications?|approvals?)\b|\bleaves?\b.*\b(?:pending|approv|today|staff|employee)", t) \
            or re.search(r"\b(?:staff|employee)s? leave", t):
        intent = "staff_leave"
    elif re.search(r"\b(headcount|head count|staff strength|how many (?:staff|employees|people work)|employees? (?:joined|left|resigned)|"
                   r"new joiners?|joiners|attrition|resigned)\b", t):
        intent = "headcount"
    # sales set against purchases, or one period against another
    elif (_COMPARE.search(t) and (_has(t, _SALES) or re.search(r"purchase|bought|\bgrn", t))) \
            or (_has(t, _SALES) and re.search(r"\b(?:and|with|&)\s+(?:purchases?|buying|grns?)\b", t)):
        intent = "compare"
    elif _has(t, ("birthday", "anniversar", "பிறந்தநாள்")):
        intent = "birthdays"
    elif _has(t, ("gift voucher", "voucher", "gift card")):
        intent = "gift_vouchers"
    elif _has(t, ("coupon",)):
        intent = "coupons"
    elif re.search(r"\bleads?\b|enquir|inquir|follow[ -]?up|prospect", t):
        intent = "leads"
    elif re.search(r"walk[ -]?ins?\b|footfall|foot fall|divert|visitors?\b", t):
        intent = "walkins"
    elif _has(t, ("feedback", "rating", "review", "complain", "stars", "satisf", "கருத்து")):
        intent = "feedback"
    elif _has(t, ("loyalty", " points", "reward")):
        intent = "customer" if _has(t, (" of ", " for ", " does ", " has ")) and \
            not _has(t, ("all customers", "total", "everyone", "held", "outstanding", "how many points were")) \
            else "loyalty"
    elif re.search(r"\borders?\b", t) and not _has(t, ("purchase", " po ", " pos", "supplier", "vendor")):
        intent = "customer_orders"
    elif _has(t, ("dead stock", "no sale", "not sold", "unsold", "not moving", "non moving",
                  "non-moving", "slow moving", "slow-moving", "no movement", " idle", "ageing",
                  "aging", "விற்காத")):
        intent = "dead_stock"
    elif _has(t, ("low stock", "low in stock", "running low", "reorder", "running out",
                  "out of stock", "short of stock", "stock out", "குறைந்த இருப்பு")):
        intent = "low_stock"
    elif _has(t, ("pending payment", "payments pending", "outstanding", "payable", " owe",
                  "unpaid", "supplier due", "dues", "yet to pay", "to be paid", "pending supplier",
                  "நிலுவை", "பாக்கி")):
        intent = "pending_payments"
    elif _has(t, ("pending grn", "grn pending", "not posted", "draft grn", "unposted",
                  "waiting for review", "pending invoice", "invoices pending", "yet to post")):
        intent = "pending_grns"
    elif _has(t, ("discount", "தள்ளுபடி")):
        intent = "discounts"
    elif _has(t, ("profit", "margin", "earning", "லாபம்")):
        intent = "profit"
    elif _has(t, (" return", "credit note", "debit note", "refund", "திரும்ப")):
        intent = "returns"
    elif _has(t, ("in transit", "transfer", "dispatch", "outward", "sent to store", "sent out")):
        intent = "transfers"
    # Customer bills at the tills — "billed by Priya", "R Kumar's bills". Not
    # "billing" (that is `sales`), and not a supplier's bill.
    elif re.search(r"\bbills?\b|\bbilled\b|\binvoiced\b", t) \
            and not _has(t, ("supplier", "vendor", " grn", "purchase", "bill entry")):
        intent = "sales_rank" if rank and group else "bills"
    elif _has(t, ("who did", "what did", "activity", "audit", " log ", "who changed",
                  "who posted", "who deleted", "actions by", "what has", "done today")) \
            and not _has(t, _SALES):
        intent = "activity"
    elif _has(t, ("payment", " paid", "paid to")) and not _has(t, _SALES):
        intent = "payments"
    elif _has(t, (" grn", "purchase", "bought", "received", " receipt", " inward", " buy ",
                  "கொள்முதல்", "வாங்கிய")):
        intent = "purchases"
    # "LR" as a word, not as two letters inside another one — `clr`, `colour` and
    # half the alphabet contain them.
    elif re.search(r"\blrs?\b|lorry receipt|consignment|transport entr|docket|"
                   r"போக்குவரத்து|லாரி", t):
        intent = "lr"
    elif _has(t, ("invoice", "bill entry", "document", "விலைப்பட்டியல்")):
        intent = "invoices"
    elif _has(t, ("customer", "client", "sign up", "signup", "sign-up", "registration", "registered",
                  "member", "வாடிக்கையாளர்")) and (rank or not _has(t, _SALES + (" return",))):
        intent = "customers"
    elif " how many " in t and _has(t, ("warehouses", "stores", "shops", "branches", "counters",
                                        "tills", " pos", "users", "people", "accounts")) \
            and not _has(t, _SALES + _STOCK):
        intent = "counts"
    elif _has(t, _STOCK):
        intent = "stock_rank" if rank and group in ("warehouse", "category") else "stock"
    elif _has(t, _SALES) or _has(t, (" sell", "business today")):
        intent = "sales_rank" if (rank or wise) and group else "sales"
    # "what products are on the ground floor" — what is kept there
    elif re.search(r"\b(?:products?|items?|goods|garments?|what)\b", t) \
            and re.search(r"\b(?:on|in|at)\s+(?:the\s+)?[a-z0-9]+\s+floor\b", t):
        intent = "stock"
    else:
        intent = NONE

    if intent == "compare":
        # "this month with last month": the period asked about is the first one;
        # the answer works out the one it is compared with
        for now_words, then_words in (("this month", "last month"), ("this week", "last week"),
                                      ("this year", "last year"), ("today", "yesterday")):
            if now_words in t and then_words in t:
                lo, hi = nlq.relative_range(now_words, today=today)
                break

    # places named in the question
    wh = _names_in(question, _warehouse_rows(db), extra="code")
    st = _names_in(question, _store_rows(db), extra="code")
    floor = ""
    fm = re.search(r"\b([a-z0-9]+(?:\s+[a-z0-9]+)?\s+floor)\b", t)
    if fm and intent not in ("sales_rank", "stock_rank") and not (wise and group == "floor"):
        floor = fm.group(1).strip()
        floor = re.sub(r"^(on|in|at|the)\s+", "", floor)
    counter = ""
    cm = re.search(r"\b(?:counter|till|pos)\s*(?:no\.?|number|#)?\s*(\d{1,3})\b", t)
    if cm:
        counter = cm.group(1)
    wh_word = ""
    if not wh:
        wm = re.search(r"\bwarehouse\s+([a-z0-9-]+)\b", t)
        if wm and wm.group(1) not in ("has", "have", "is", "with", "in", "sell", "sold", "stock"):
            wh_word = f"warehouse {wm.group(1)}"

    item = ""
    im = re.search(r"\b(?:of|for)\s+([a-z][a-z&'\- ]{2,40}?)(?=\s+(?:this|last|today|yesterday|in|on|"
                   r"during|from|at|between|since|over|for|above|below|more|less)\b|[?.!,]|\s*$)", t)
    if im and intent in ("sales", "stock", "profit", "low_stock", "dead_stock", "sales_rank",
                         "pending_payments", "payments"):
        cand = im.group(1).strip()
        if not re.match(r"^(the day|today|yesterday|this|last|all|every|us|our|me|sale|sales)\b", cand) \
                and cand not in (floor,) and not _names_in(cand, _warehouse_rows(db) + _store_rows(db)):
            item = cand
    if intent in ("pending_payments", "payments") and not item:
        sm = re.search(r"\b(?:to|from)\s+([a-z][a-z0-9&.' -]{2,40}?)\s*[?.!]?\s*$", t)
        if sm:
            item = sm.group(1).strip()

    if intent == "customer" and not code:
        cm2 = re.search(r"\b(?:of|for|does|has|is|about|customer)\s+([a-z][a-z.' ]{1,40}?)"
                        r"(?=\s+(?:have|has|got|earn|hold|points|loyalty)\b|\s*[?.!]?\s*$)", t)
        if cm2 and cm2.group(1).strip() not in ("the", "a", "this", "that", "all", "my", "our"):
            item = cm2.group(1).strip()

    person = person_role = ""
    if intent in ("activity", "discounts"):
        pm = re.search(r"\b(?:did|by|has|of)\s+([a-z][a-z.]{1,20})\b(?=\s+(?:do|done|change|post|today|yesterday|this)|\s*[?.!]?\s*$)", t)
        if pm and pm.group(1) not in ("we", "you", "they", "the", "i", "it", "people", "someone", "anyone"):
            person = pm.group(1)
    if intent in _PERSONAL:
        person, person_role = _person_in(t)
        if person and _norm(item) in (_norm(person), f"customer {_norm(person)}"):
            item = ""                          # "sales of Priya" is a person, not a product

    return {
        "intent": intent, "reading": "matched on keywords",
        "date_from": lo or "", "date_to": hi or "",
        "warehouse": wh[0]["name"] if wh else wh_word, "store": st[0]["name"] if st else "",
        "floor": floor, "counter": counter, "person": person, "person_role": person_role,
        "item": item,
        "code": code or "", "group_by": ("cashier" if intent == "discounts" and not group else group)
        if intent in ("sales_rank", "stock_rank", "discounts")
        or (intent == "compare" and group in ("floor", "product", "category", "store", "day")) else "",
        "order": "bottom" if _has(t, _BOTTOM) else ("top" if rank and not wise else ""),
        "days": m_days.group(1) if m_days else "",
        "confidence": "medium" if intent != NONE else "low",
        "engine": "keywords",
    }


# ---------------------------------------------------------------------------
#  Reading the question — the model
# ---------------------------------------------------------------------------
SYSTEM = """You read an Indian garment business owner's question and route it to ONE \
intent from a fixed list, filling in the period, the place and the thing asked about. \
You never compute figures — the system does.

The question may be English or Tamil, typed or spoken (so expect speech-recognition \
spellings: "grn" as "g r n", numbers as words).

INTENTS (key: what it answers):
{intents}
- none: nothing above answers it.

KNOWN NAMES (match what the person said to these, spelling as listed):
Warehouses: {warehouses}
Stores: {stores}
Store floors: {floors}
POS counters: {counters}

RULES
1. Pick the single best intent. A wrong intent is worse than "none".
2. Dates: resolve relative phrases against TODAY to ISO YYYY-MM-DD in date_from/date_to. \
Leave both empty when no period is mentioned — the system applies its own default.
3. warehouse / store / floor / counter: the name as listed above when the question names \
one, else empty. person: a person's name if the question is about someone. item: the \
product or category asked about in the person's own words ("ladies shirts"), else empty. \
code: a document number or tag code when intent is trace. For `customer`, put the \
mobile number, customer code or loyalty card in code, or the customer's name in person.
person_role, for sales / sales_rank / bills / profit about one person: "cashier" when they \
BILLED it ("billed by Priya"), "salesperson" when they SOLD or served it on the floor, \
"customer" when they BOUGHT it ("R Kumar's bills", "sold to Ravi"), else empty for staff \
in either role. A question that is only somebody's or something's name ("Priya", "details \
of AMS Garments") is intent none with that name in person — the system looks it up \
everywhere.
4. group_by: only for sales_rank / stock_rank / discounts — what is being ranked.
5. order: "top" for highest/most/best, "bottom" for lowest/least, else empty.
6. days: a number of days mentioned (e.g. dead stock "above 90 days"), else empty.
7. reading: one short plain-English line saying how you read the question.
8. Any field that does not apply is an empty string.

TODAY is {today}."""


def _schema():
    s = lambda d: {"type": "string", "description": d}          # noqa: E731
    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": sorted(INTENTS) + [NONE]},
            "reading": s("How the question was read, one line."),
            "date_from": s("ISO date or empty"), "date_to": s("ISO date or empty"),
            "warehouse": s("Warehouse name or empty"), "store": s("Store name or empty"),
            "floor": s("Store floor name or empty"), "counter": s("POS counter name or empty"),
            "person": s("Person's name or empty"),
            "person_role": {"type": "string", "enum": [""] + list(ROLES)},
            "item": s("Product/category words or empty"),
            "code": s("Document number or tag code, or empty"),
            "group_by": {"type": "string", "enum": [""] + list(GROUPS)},
            "order": {"type": "string", "enum": ["", "top", "bottom"]},
            "days": s("Number of days or empty"),
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["intent", "reading", "date_from", "date_to", "warehouse", "store",
                     "floor", "counter", "person", "person_role", "item", "code", "group_by",
                     "order", "days", "confidence"],
        "additionalProperties": False,
    }


def _ask_model(db, question):
    import anthropic
    client = anthropic.Anthropic(api_key=runtime.get("anthropic_api_key"))
    names = lambda rows, k="name": ", ".join(sorted({str(r[k]) for r in rows if r.get(k)})[:80]) or "(none)"  # noqa: E731
    system = SYSTEM.format(
        intents="\n".join(f"- {k}: {v}" for k, v in INTENTS.items()),
        warehouses=names(_warehouse_rows(db)), stores=names(_store_rows(db)),
        floors=names(pos_insights.floors()), counters=names(pos_insights.counters()),
        today=business_day.today().isoformat())
    msg = client.messages.create(
        model=nlq.NLQ_MODEL, max_tokens=4096,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": question}],
        # see services/nlq for why output_config rides in extra_body
        extra_body={"output_config": {"effort": "low",
                                      "format": {"type": "json_schema", "schema": _schema()}}},
    )
    if msg.stop_reason == "refusal":
        raise RuntimeError("the model declined to answer this question")
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    data = json.loads(text)
    data["engine"] = "model"
    return data


def interpret(db, question):
    """The reading of a question. A code short-circuits both readers. Never raises."""
    code = _code_in(question)
    if code:
        out = _offline(db, question)
        out.update(intent="trace", code=code, reading=f"track {code} end to end",
                   confidence="high")
        return out
    if nlq.available():
        try:
            return _ask_model(db, question)
        except Exception as e:                         # noqa: BLE001
            out = _offline(db, question)
            out["degraded"] = f"the model could not be reached ({type(e).__name__}); matched on keywords instead"
            return out
    return _offline(db, question)


# ---------------------------------------------------------------------------
#  Period and place
# ---------------------------------------------------------------------------
def _period(read, default="today"):
    d0 = business_day.parse_day(read.get("date_from"))
    d1 = business_day.parse_day(read.get("date_to"))
    today = business_day.today()
    if not d0 and not d1:
        if default is None:
            return None
        d0 = d1 = today if default == "today" else today
        if default == "month":
            d0 = today.replace(day=1)
    d0, d1 = d0 or d1, d1 or d0
    if d1 < d0:
        d0, d1 = d1, d0
    lbl = business_day.label(d0, d1)
    start, end = business_day.bounds(d0, d1)
    return {"from": d0.isoformat(), "to": d1.isoformat(), "label": lbl,
            "start": start, "end": end}


def _titled(label, noun):
    """Today's Sales / Yesterday's Sales / Sales — This month."""
    if label in ("Today", "Yesterday"):
        return f"{label}'s {noun}"
    return f"{noun} — {label}"


def _phrase(label):
    """today / yesterday / this month / on 05-09-2026 / from … to …"""
    if label in ("Today", "Yesterday", "This month", "Last month") or label.startswith("Last "):
        return label.lower()
    if " to " in label:
        return f"from {label}"
    return f"on {label}"


def _best(text, rows, key="name", cutoff=0.72):
    if not text:
        return None
    want = _norm(text)
    for r in rows:
        if _norm(r.get(key)) == want or (r.get("code") and _norm(r["code"]) == want):
            return r
    for r in rows:
        n = _norm(r.get(key))
        if n and (want in n or n in want):
            return r
    names = {_norm(r.get(key)): r for r in rows if r.get(key)}
    close = difflib.get_close_matches(want, list(names), n=1, cutoff=cutoff)
    return names[close[0]] if close else None


class Refused(Exception):
    pass


def _scope(db, read, allowed):
    """Resolve the places a question names, inside what the account may see.

    Returns {wh_ids, at, place, notes, store, floor_ids, warehouse}. Raises Refused
    for a named warehouse or store the account is not allotted."""
    notes, place = [], None
    wh = st = None
    at = {}
    if read.get("warehouse"):
        wh = _best(read["warehouse"], _warehouse_rows(db))
        if wh is None:
            notes.append(f"No warehouse called “{read['warehouse']}” — answered for "
                         + ("all your warehouses." if allowed else "every warehouse."))
        elif allowed and wh["id"] not in allowed:
            raise Refused(f"You are not allotted the warehouse “{wh['name']}”.")
    if read.get("store"):
        st = _best(read["store"], _store_rows(db))
        if st is None:
            notes.append(f"No store called “{read['store']}” — the store filter was left off.")
        elif allowed and st["warehouse_id"] not in allowed:
            raise Refused(f"The store “{st['name']}” belongs to a warehouse you are not allotted.")

    if st:
        at["location_ids"] = pos_insights.location_ids_for_stores(
            db.query(models.Store).filter(models.Store.id == st["id"]).all())
        place = st["name"]
    elif wh:
        at["location_ids"] = pos_insights.location_ids_for_warehouses(db, [wh["id"]])
        place = wh["name"]
    elif allowed:
        at["location_ids"] = pos_insights.location_ids_for_warehouses(db, allowed)

    floor_ids = None
    if read.get("floor"):
        want = _norm(read["floor"])
        fl = pos_insights.floors()
        hits = [f for f in fl if _norm(f["name"]) == want] or \
               [f for f in fl if want in _norm(f["name"]) or _norm(f["name"]) in want]
        if at.get("location_ids") is not None:
            hits = [f for f in hits if f["location_id"] in at["location_ids"]]
        if hits:
            floor_ids = [f["id"] for f in hits]
            at["floor_ids"] = floor_ids
            place = hits[0]["name"] + (f" ({place})" if place else "")
        else:
            notes.append(f"No store floor called “{read['floor']}”.")
            floor_ids = []
            at["floor_ids"] = []
    if read.get("counter"):
        want = _norm(read["counter"])
        cs = pos_insights.counters()
        hits = [c for c in cs if _norm(c["name"]) == want
                or re.search(rf"(?<!\d)0*{re.escape(want)}(?!\d)", _norm(c["name"]) or "")]
        if at.get("location_ids") is not None:
            hits = [c for c in hits if c["location_id"] in at["location_ids"]]
        if hits:
            at["counter_ids"] = [c["id"] for c in hits]
            place = hits[0]["name"] + (f", {hits[0]['branch']}" if hits[0].get("branch") else "")
        else:
            notes.append(f"No POS counter called “{read['counter']}”.")
    return {"wh_ids": [wh["id"]] if wh else (list(allowed) if allowed else None),
            "at": at or None, "place": place, "notes": notes, "store": st,
            "warehouse": wh, "floor_ids": floor_ids}


# ---------------------------------------------------------------------------
#  Answers
# ---------------------------------------------------------------------------
def _out(title, headline, line, speak=None, facts=None, columns=None, rows=None,
         note=None, open_=None, ok=True):
    return {"ok": ok, "title": title, "headline": headline, "line": line,
            "speak": speak or re.sub(r"₹\s?([\d,\.]+)", r"\1 rupees", line),
            "facts": [{"label": k, "value": v} for k, v in (facts or []) if v not in (None, "")],
            "columns": columns or [], "rows": rows or [], "note": note, "open": open_}


def _no_pos():
    return _out("Store sales", "—",
                "The stores' till database cannot be read from here, so there are no store figures to answer with.",
                ok=False)


def _local_time(ts):
    loc = business_day.local(ts)
    return loc.strftime("%d-%m-%Y %H:%M") if loc else ""


def a_sales(db, read, sc, per):
    t = pos_insights.totals(per["start"], per["end"], sc["at"], read.get("item") or None)
    if not t.get("available"):
        return _no_pos()
    item = (read.get("item") or "").strip()
    stores = pos_insights.breakdown(per["start"], per["end"], "store", sc["at"], item or None)
    bw = pos_insights.branch_warehouses(db)
    whs = {bw[_norm(r["label"])][0] for r in stores if _norm(r["label"]) in bw}
    n_stores = len([r for r in stores if r["label"] != "(no branch)"])
    n_counters = t.get("counters") if not item else len(
        pos_insights.breakdown(per["start"], per["end"], "counter", sc["at"], item))
    noun = (f"Sales of {item.title()}" if item else "Sales") + _who_words(sc)
    title = _titled(per["label"], noun)
    if sc["place"]:
        title += f" at {sc['place']}"
    if item:
        line = (f"{title}: {inr(t['net'])} — {plural(t['units'], 'piece')} of "
                f"{plural(t.get('products', 0), 'product')} on {plural(t['bills'], 'bill')}.")
        speak = f"{title}: {spoken_inr(t['net'])}, {fmt_qty(t['units'])} pieces."
    elif sc["place"] or sc.get("who"):
        line = f"{title}: {inr(t['net'])} on {plural(t['bills'], 'bill')} — {plural(t['units'], 'unit')}."
        speak = f"{title}: {spoken_inr(t['net'])}."
    else:
        line = (f"{title}: {inr(t['net'])} across {plural(len(whs), 'warehouse')}, "
                f"{plural(n_stores, 'store')} and {plural(n_counters or 0, 'POS counter')}.")
        speak = f"{title}: {spoken_inr(t['net'])} across {plural(len(whs), 'warehouse')}."
    facts = [("Bills", fmt_qty(t["bills"])), ("Units", fmt_qty(t["units"])),
             ("Billed", inr(t["gross"])), ("Returns", inr(t["returns"]))]
    if not item:
        facts += [("Discount", inr(t["discount"])), ("Tax", inr(t["tax"]))]
    rows = [{"Store": r["label"], "Warehouse": (bw.get(_norm(r["label"])) or (None, None))[1] or "—",
             "Bills": r["bills"], "Sales": r["amount"]} for r in stores]
    note = ("Sales are net of customer returns made in the same period."
            + (" Line values include tax; a whole-bill discount cannot be split onto lines."
               if item else ""))
    return _out(title, inr(t["net"]), line, speak, facts, ["Store", "Warehouse", "Bills", "Sales"],
                rows, note, {"tab": "central"})


_GROUP_NOUN = {"warehouse": "Warehouse", "store": "Store", "floor": "Floor", "counter": "Counter",
               "cashier": "Cashier", "staff": "Salesperson", "category": "Category",
               "product": "Product", "payment": "Payment method", "day": "Day"}


def a_sales_rank(db, read, sc, per):
    if not pos_insights.available():
        return _no_pos()
    by = read.get("group_by") or "store"
    item = read.get("item") or None
    if by == "warehouse":
        bw = pos_insights.branch_warehouses(db)
        agg = {}
        for r in pos_insights.breakdown(per["start"], per["end"], "store", sc["at"], item):
            name = (bw.get(_norm(r["label"])) or (None, "(store not matched)"))[1] or "(store not matched)"
            a = agg.setdefault(name, {"label": name, "bills": 0, "amount": 0.0})
            a["bills"] += r["bills"]
            a["amount"] = round(a["amount"] + r["amount"], 2)
        rows = sorted(agg.values(), key=lambda x: -x["amount"])
    else:
        rows = pos_insights.breakdown(per["start"], per["end"], by, sc["at"], item)
    rows = [r for r in rows if r["amount"]]
    noun = _GROUP_NOUN.get(by, by.title())
    if not rows:
        line = f"No store sales {_phrase(per['label'])} to rank by {noun.lower()}."
        return _out(f"Sales by {noun}", "—", line, ok=True)
    bottom = read.get("order") == "bottom"
    if by == "day":                                   # a day-wise list reads in date order
        rows.sort(key=lambda r: r["label"])
        for r in rows:
            r["label"] = _local_day(r["label"])
    pick = (min(rows, key=lambda r: r["amount"]) if bottom else max(rows, key=lambda r: r["amount"]))
    total = sum(r["amount"] for r in rows) or 1
    word = "Lowest" if bottom else "Highest"
    who = f" ({_who_words(sc).strip()})" if sc.get("who") else ""
    if read.get("order"):
        title = f"{word} Sales by {noun} — {per['label']}{who}"
        line = f"{word} Sales {_phrase(per['label'])}: {pick['label']} — {inr_short(pick['amount'])}."
    else:
        # asked "product wise" — the whole list, with the best named
        title = f"{noun} wise Sales — {per['label']}{who}"
        line = (f"{noun} wise sales {_phrase(per['label'])}: {inr_short(sum(r['amount'] for r in rows))} across "
                f"{plural(len(rows), noun.lower())}; the most was {pick['label']} — {inr_short(pick['amount'])}.")
    speak = f"{word} sales: {pick['label']}, {spoken_inr(pick['amount'])}."
    ranked = sorted(rows, key=lambda r: r["amount"], reverse=not bottom)
    others = ranked[1] if len(ranked) > 1 else None
    facts = [("Share", f"{pick['amount'] / total * 100:.1f}%"), ("Bills", fmt_qty(pick["bills"])),
             ("Next", f"{others['label']} — {inr_short(others['amount'])}" if others else None),
             (f"{noun}s selling", fmt_qty(len(rows))), ("Total", inr(sum(r["amount"] for r in rows)))]
    with_qty = any(r.get("qty") is not None for r in rows)
    cols = [noun, "Bills"] + (["Qty"] if with_qty else []) + ["Sales", "Share %"]
    return _out(title, inr_short(pick["amount"]), line, speak, facts, cols,
                [{noun: r["label"], "Bills": r["bills"], **({"Qty": r.get("qty")} if with_qty else {}),
                  "Sales": r["amount"], "Share %": round(r["amount"] / total * 100, 1)} for r in rows],
                None, {"tab": "central"})


# ---------------------------------------------------------------------------
#  Comparisons — sales against purchases, one period against another
# ---------------------------------------------------------------------------
def _pct_change(now, then):
    return round((now - then) / then * 100, 1) if then else None


def _change_words(now, then):
    if not then:
        return "nothing to compare with" if not now else "all new"
    pc = _pct_change(now, then)
    return f"{'up' if pc >= 0 else 'down'} {abs(pc):g}%"


def _previous_period(read, per):
    """The period a comparison is made against: the same days of the month (or
    week) before — so the 1st–4th is set against the 1st–4th — or the same dates
    a year earlier when last year is asked for."""
    d0, d1 = business_day.parse_day(per["from"]), business_day.parse_day(per["to"])
    t = f" {(read.get('question') or '').lower()} "
    if "last year" in t and "this year" not in t:
        def back(d):
            try:
                return d.replace(year=d.year - 1)
            except ValueError:
                return d.replace(year=d.year - 1, day=28)
        p0, p1 = back(d0), back(d1)
    elif d0.day == 1 and d0.month == d1.month and d0.year == d1.year:
        last = d0 - timedelta(days=1)                       # the month before, same days
        p0 = last.replace(day=1)
        p1 = min(last, p0 + (d1 - d0))
    else:
        n = (d1 - d0).days + 1
        p1 = d0 - timedelta(days=1)
        p0 = p1 - timedelta(days=n - 1)
    start, end = business_day.bounds(p0, p1)
    return {"from": p0.isoformat(), "to": p1.isoformat(), "label": business_day.label(p0, p1),
            "start": start, "end": end}


def _purchases_in(db, sc, per):
    """Posted GRNs whose supplier invoice falls in the period, in the account's warehouses."""
    q =db.query(models.Purchase).filter(models.Purchase.status == "posted")
    if sc["wh_ids"]:
        q = q.filter(models.Purchase.warehouse_id.in_(sc["wh_ids"]))
    out = []
    for p in q.all():
        d = date_svc.to_iso(p.invoice_date)
        if d and per["from"] <= d <= per["to"]:
            out.append((d, float(p.grand_total or 0)))
    return out


def a_compare(db, read, sc, per):
    if not pos_insights.available():
        return _no_pos()
    t = f" {(read.get('question') or '').lower()} "
    by = read.get("group_by") or ""
    today = business_day.today()
    if business_day.parse_day(per["to"]) > today >= business_day.parse_day(per["from"]):
        # "this month" runs to the 30th; what has happened is to today, and that is
        # what is set against the month before
        per = _period({"date_from": per["from"], "date_to": today.isoformat()})
    if re.search(r"purchase|bought|\bgrns?\b|receiv|inward|buying", t):
        return _sales_vs_purchase(db, sc, per, by)
    return _period_compare(read, sc, per, by)


def _sales_vs_purchase(db, sc, per, by):
    when = _phrase(per["label"])
    if by in ("floor", "product"):
        f = pos_insights.flow(per["start"], per["end"], by, sc["at"])
        noun = "Floor" if by == "floor" else "Product"
        rows = f.get("rows") or []
        if not rows:
            return _out(f"Sales vs Purchase by {noun}", "—", f"Nothing was received or sold {when}.")
        recv, sales = sum(r["received_value"] for r in rows), sum(r["sales"] for r in rows)
        top = rows[0]
        title = _titled(per["label"], f"{noun} wise Sales vs Purchase")
        line = (f"{title}: {inr_short(sales)} sold against {inr_short(recv)} of stock received at cost"
                + (f"; {top['label']} sold the most — {inr_short(top['sales'])}." if top["sales"] else "."))
        cols = ([noun] + (["SKU"] if by == "product" else [])
                + ["Received", "Received value", "Sold", "Sales", "In stock now", "Stock value"])
        out_rows = [{noun: r["label"], **({"SKU": r["sku"]} if by == "product" else {}),
                     "Received": r["received"], "Received value": round(r["received_value"], 2),
                     "Sold": r["sold"], "Sales": round(r["sales"], 2), "In stock now": r["stock"],
                     "Stock value": round(r["stock_value"], 2)} for r in rows[:300]]
        return _out(title, inr_short(sales), line, None,
                    [("Sales", inr(sales)), ("Received (at cost)", inr(recv)),
                     ("Pieces sold", fmt_qty(sum(r["sold"] for r in rows))),
                     ("Pieces received", fmt_qty(sum(r["received"] for r in rows)))],
                    cols, out_rows,
                    "In the stores \"purchase\" is stock RECEIVED — transfers counted in from the warehouse plus "
                    "opening entries. Placed on the floor each product is kept on. Store → Reports → Comparison "
                    "Reports has the same by floor and by product.", {"tab": "pos:reports"})
    # the business as a whole: what the warehouses bought, what the shops sold
    bought = _purchases_in(db, sc, per)
    days = {r["label"]: r for r in pos_insights.breakdown(per["start"], per["end"], "day", sc["at"])}
    pv = round(sum(v for _, v in bought), 2)
    t = pos_insights.totals(per["start"], per["end"], sc["at"])
    sv = t.get("net", 0.0)
    title = _titled(per["label"], "Sales vs Purchase")
    if sc["place"]:
        title += f" at {sc['place']}"
    line = (f"{title}: sales {inr_short(sv)} against purchases {inr_short(pv)}"
            + (f" — sales are {sv / pv * 100:.0f}% of purchases." if pv else " — nothing was purchased."))
    per_day = {}
    for d, v in bought:
        a = per_day.setdefault(d, [0, 0.0, 0, 0.0])
        a[0] += 1
        a[1] += v
    for d, r in days.items():
        a = per_day.setdefault(d, [0, 0.0, 0, 0.0])
        a[2], a[3] = r["bills"], r["amount"]
    rows = [{"Day": _local_day(d), "GRNs": a[0], "Purchases": round(a[1], 2), "Bills": a[2],
             "Sales": round(a[3], 2), "Difference": round(a[3] - a[1], 2)} for d, a in sorted(per_day.items())]
    return _out(title, inr_short(sv - pv), line, f"Sales {spoken_inr(sv)} against purchases {spoken_inr(pv)}.",
                [("Sales (net)", inr(sv)), ("Purchases (GRN)", inr(pv)), ("Difference", inr(sv - pv)),
                 ("GRNs", fmt_qty(len(bought))), ("Bills", fmt_qty(t.get("bills", 0)))],
                ["Day", "GRNs", "Purchases", "Bills", "Sales", "Difference"], rows,
                "Purchases are posted GRNs by supplier-invoice date, at the bill's total. Sales are the stores' "
                "takings net of returns; a day's figure is the bills raised that day. Reports → Sales Vs "
                "Purchase - Daily has the same.", {"tab": "reports"})


def _period_compare(read, sc, per, by):
    prev = _previous_period(read, per)
    noun = _GROUP_NOUN.get(by, "") if by else ""
    title = (f"{noun} wise " if noun else "") + f"Sales — {per['label']} vs {prev['label']}"
    if by and by != "day":
        now = {r["label"]: r for r in pos_insights.breakdown(per["start"], per["end"], by, sc["at"])}
        then = {r["label"]: r for r in pos_insights.breakdown(prev["start"], prev["end"], by, sc["at"])}
        rows = []
        for k in set(now) | set(then):
            a, b = now.get(k, {}).get("amount", 0.0), then.get(k, {}).get("amount", 0.0)
            rows.append({noun: k, per["label"]: a, prev["label"]: b, "Change": round(a - b, 2),
                         "Change %": _pct_change(a, b)})
        rows.sort(key=lambda r: -r[per["label"]])
        sn, sp = sum(r[per["label"]] for r in rows), sum(r[prev["label"]] for r in rows)
        grew = max(rows, key=lambda r: r["Change"]) if rows else None
        fell = min(rows, key=lambda r: r["Change"]) if rows else None
        line = (f"{title}: {inr_short(sn)} against {inr_short(sp)}, {_change_words(sn, sp)}"
                + (f"; {grew[noun]} grew most ({inr_short(grew['Change'])})" if grew and grew["Change"] > 0 else "")
                + (f"; {fell[noun]} fell most ({inr_short(fell['Change'])})" if fell and fell["Change"] < 0 else "")
                + ".")
        cols = [noun, per["label"], prev["label"], "Change", "Change %"]
    else:
        a = pos_insights.totals(per["start"], per["end"], sc["at"])
        b = pos_insights.totals(prev["start"], prev["end"], sc["at"])
        sn, sp = a.get("net", 0.0), b.get("net", 0.0)
        line = f"{title}: {inr_short(sn)} against {inr_short(sp)} — {_change_words(sn, sp)}."
        rows = [{"": lbl, per["label"]: x, prev["label"]: y, "Change": round(x - y, 2), "Change %": _pct_change(x, y)}
                for lbl, x, y in (("Sales (net)", sn, sp), ("Bills", a.get("bills", 0), b.get("bills", 0)),
                                  ("Units", a.get("units", 0), b.get("units", 0)),
                                  ("Discount", a.get("discount", 0), b.get("discount", 0)),
                                  ("Returns", a.get("returns", 0), b.get("returns", 0)))]
        cols = ["", per["label"], prev["label"], "Change", "Change %"]
    return _out(title, _change_words(sn, sp), line, None,
                [(per["label"], inr(sn)), (prev["label"], inr(sp)), ("Change", inr(sn - sp))],
                cols, rows, f"Compared with {_local_day(prev['from'])} to {_local_day(prev['to'])}.",
                {"tab": "central"})


def a_discounts(db, read, sc, per):
    if not pos_insights.available():
        return _no_pos()
    by = "staff" if read.get("group_by") == "staff" else "cashier"
    rows = pos_insights.discounts(per["start"], per["end"], by, sc["at"])
    if read.get("person"):
        want = _norm(read["person"])
        rows = [r for r in rows if want in _norm(r["label"])] or rows
    title = _titled(per["label"], "Discounts")
    if not rows:
        return _out(title, inr(0), f"No discounts were given {_phrase(per['label'])}.")
    total = sum(r["discount"] for r in rows)
    bills = sum(r["bills"] for r in rows)
    top = rows[0]
    line = (f"Highest Discount {_phrase(per['label'])}: {top['label']} — {inr(top['discount'])} "
            f"on {plural(top['bills'], 'bill')} (all discounts: {inr(total)} on {plural(bills, 'bill')}).")
    speak = f"Highest discount: {top['label']}, {spoken_inr(top['discount'])}."
    return _out(title, inr(top["discount"]), line, speak,
                [("Total discount", inr(total)), ("Bills discounted", fmt_qty(bills)),
                 ("Of which coupons", inr(sum(r["coupon"] for r in rows)))],
                ["Given by", "Bills", "Discount", "Coupons", "Billed"],
                [{"Given by": r["label"], "Bills": r["bills"], "Discount": r["discount"],
                  "Coupons": r["coupon"], "Billed": r["gross"]} for r in rows], None, None)


def a_profit(db, read, sc, per):
    m = pos_insights.margin(per["start"], per["end"], sc["at"], read.get("item") or None)
    if not m.get("available"):
        return _no_pos()
    title = _titled(per["label"], "Profit" + _who_words(sc))
    if sc["place"]:
        title += f" at {sc['place']}"
    pct = f"{m['margin_pct']}%" if m["margin_pct"] is not None else "—"
    line = (f"{title}: {inr_short(m['profit'])} — {pct} margin on {inr_short(m['revenue'])} "
            "of sales before tax.")
    note = "Cost is each product's current cost at the till (the warehouse's average cost)."
    if m["uncosted_revenue"]:
        note += (f" {inr(m['uncosted_revenue'])} of the sales are of products with no cost price, "
                 "so their margin is overstated.")
    return _out(title, inr_short(m["profit"]), line,
                f"{title}: {spoken_inr(m['profit'])}, {pct} margin.",
                [("Sales before tax", inr(m["revenue"])), ("Cost of goods", inr(m["cost"])),
                 ("Margin", pct)], note=note)


def a_purchases(db, read, sc, per):
    p = cc.purchases_between(db, per["start"], per["end"], sc["wh_ids"])
    title = _titled(per["label"], "GRN Receipts")
    if sc["warehouse"]:
        title += f" at {sc['warehouse']['name']}"
    if not p["grns"]:
        return _out(title, "0", f"No GRNs were posted {_phrase(per['label'])}.",
                    open_={"tab": "purchases"})
    line = (f"{title}: {fmt_qty(p['units'])} units of {plural(p['products'], 'product')} on "
            f"{plural(p['grns'], 'GRN')}, worth {inr(p['value'])}.")
    rows = [{"GRN": r.grn_no or f"#{r.id}", "Supplier": r.supplier.name if r.supplier else "—",
             "Warehouse": r.warehouse.name if r.warehouse else "—",
             "Units": round(sum(float(l.qty or 0) for l in r.lines), 3),
             "Value": r.grand_total, "Posted": _local_time(r.posted_at)} for r in p["rows"]]
    return _out(title, fmt_qty(p["units"]), line,
                f"{title}: {fmt_qty(p['units'])} units on {plural(p['grns'], 'GRN')}, worth {spoken_inr(p['value'])}.",
                [("GRNs", fmt_qty(p["grns"])), ("Products", fmt_qty(p["products"])),
                 ("Value", inr(p["value"]))],
                ["GRN", "Supplier", "Warehouse", "Units", "Value", "Posted"], rows,
                "Counted by the day each GRN was POSTED — the day the goods became stock.",
                {"tab": "purchases"})


def a_invoices(db, read, sc, per):
    """Supplier invoices entered — and, because the word means two things in a
    shop, what the tills billed said beside it rather than instead of it."""
    q = db.query(models.Document).filter(models.Document.uploaded_at >= per["start"],
                                         models.Document.uploaded_at < per["end"])
    if sc["wh_ids"]:
        q = q.filter(models.Document.warehouse_id.in_(sc["wh_ids"]))
    rows = q.order_by(models.Document.uploaded_at.desc()).all()
    by_status = {}
    for d in rows:
        by_status[d.status] = by_status.get(d.status, 0) + 1
    waiting = by_status.get("needs_review", 0) + by_status.get("extracted", 0) \
        + by_status.get("uploaded", 0)
    done = by_status.get("confirmed", 0) + by_status.get("posted", 0)
    title = _titled(per["label"], "Invoice Entries")
    if sc["warehouse"]:
        title += f" at {sc['warehouse']['name']}"
    # Document carries a warehouse_id and no relationship to go with it, so the
    # names are looked up once rather than per row.
    wh_names = {w.id: w.name for w in db.query(models.Warehouse).all()}
    sales = pos_insights.totals(per["start"], per["end"], sc["at"]) if pos_insights.available() else {}
    line = (f"{title}: {plural(len(rows), 'supplier invoice')}"
            + (f" — {waiting} still to review, {done} confirmed." if rows else " — none came in."))
    if sales.get("bills"):
        line += (f" The tills billed {plural(sales['bills'], 'customer invoice')} "
                 f"worth {inr(sales['net'])}.")
    return _out(title, fmt_qty(len(rows)), line,
                f"{title}: {plural(len(rows), 'supplier invoice')}, {waiting} still to review.",
                [("To review", fmt_qty(waiting)), ("Confirmed or posted", fmt_qty(done)),
                 ("Customer bills", fmt_qty(sales.get("bills", 0)) if sales else None)],
                ["Invoice", "Supplier", "Status", "Warehouse", "Entered"],
                [{"Invoice": d.filename, "Supplier": d.supplier.name if d.supplier else "—",
                  "Status": d.status, "Warehouse": wh_names.get(d.warehouse_id, "—"),
                  "Entered": _local_time(d.uploaded_at)} for d in rows],
                "Counted by when the invoice was ENTERED here, not by the date the "
                "supplier printed on it.", {"tab": "documents"})


def a_lr(db, read, sc, per):
    """Consignments booked into the transport register."""
    q = db.query(models.LREntry).filter(models.LREntry.created_at >= per["start"],
                                        models.LREntry.created_at < per["end"])
    if sc["wh_ids"]:
        q = q.filter(models.LREntry.warehouse_id.in_(sc["wh_ids"]))
    rows = q.order_by(models.LREntry.id.desc()).all()
    pieces = round(sum(float(e.qty or 0) for e in rows), 3)
    value = round(sum(float(e.amount or 0) for e in rows), 2)
    unreceived = [e for e in rows if not (e.received_by or "").strip()]
    title = _titled(per["label"], "LR Entries")
    if sc["warehouse"]:
        title += f" at {sc['warehouse']['name']}"
    line = (f"{title}: {plural(len(rows), 'consignment')}"
            + (f" — {fmt_qty(pieces)} pieces worth {inr(value)}"
               + (f"; {len(unreceived)} not received yet." if unreceived else ", all received.")
               if rows else " — nothing was booked in."))
    return _out(title, fmt_qty(len(rows)), line,
                f"{title}: {plural(len(rows), 'consignment')}, {fmt_qty(pieces)} pieces.",
                [("Pieces", fmt_qty(pieces)), ("Goods value", inr(value)),
                 ("Not received", fmt_qty(len(unreceived)))],
                ["LR no", "Entry no", "Supplier", "Transport", "Pieces", "Value", "Received"],
                [{"LR no": e.lr_no, "Entry no": e.lr_entry_no, "Supplier": e.supplier_name,
                  "Transport": e.transport, "Pieces": e.qty, "Value": e.amount,
                  "Received": e.received_by or "—"} for e in rows],
                None, {"tab": "lr"})


def a_payments(db, read, sc, per):
    pays = cc.payments_between(db, per["start"], per["end"])
    rows = pays["rows"]
    who = (read.get("item") or read.get("person") or "").strip()
    if who:
        rows = [p for p in rows if p.supplier and _norm(who) in _norm(p.supplier.name)] or rows
    total = round(sum(float(p.paid_amount or 0) for p in rows), 2)
    title = _titled(per["label"], "Supplier Payments")
    line = (f"{title}: {inr(total)} across {plural(len(rows), 'payment')}."
            if rows else f"No supplier payments were recorded {_phrase(per['label'])}.")
    return _out(title, inr(total), line, None,
                [("Payments", fmt_qty(len(rows)))],
                ["Receipt", "Supplier", "Mode", "Paid", "Discount", "TDS", "Recorded"],
                [{"Receipt": p.receipt_no, "Supplier": p.supplier.name if p.supplier else "—",
                  "Mode": p.mode, "Paid": p.paid_amount, "Discount": p.discount_total,
                  "TDS": p.tds_total, "Recorded": _local_time(p.created_at)} for p in rows],
                "Supplier payments are company-wide — they are not filed under a warehouse.",
                {"tab": "payments"})


def a_pending_payments(db, read, sc, per):
    owed = cc.outstanding(db, supplier_name=(read.get("item") or read.get("person") or None))
    if not owed and (read.get("item") or read.get("person")):
        owed = cc.outstanding(db)
    agg = {}
    for b in owed:
        a = agg.setdefault(b["supplier"], {"Supplier": b["supplier"], "Bills": 0, "Outstanding": 0.0,
                                           "Oldest (days)": 0})
        a["Bills"] += 1
        a["Outstanding"] = round(a["Outstanding"] + b["outstanding"], 2)
        a["Oldest (days)"] = max(a["Oldest (days)"], b["days"] or 0)
    rows = sorted(agg.values(), key=lambda r: -r["Outstanding"])
    total = round(sum(b["outstanding"] for b in owed), 2)
    overdue = round(sum(b["outstanding"] for b in owed if (b["days"] or 0) > 30), 2)
    title = "Pending Supplier Payments"
    if not owed:
        return _out(title, inr(0), "Nothing is owed to suppliers — every posted bill is settled.",
                    open_={"tab": "payments"})
    line = (f"{title}: {inr_short(total)} across {plural(len(owed), 'bill')} from "
            f"{plural(len(rows), 'supplier')}"
            + (f" — {inr_short(overdue)} is more than 30 days old." if overdue else "."))
    return _out(title, inr_short(total), line,
                f"Pending supplier payments: {spoken_inr(total)} across {plural(len(owed), 'bill')}.",
                [("Largest", f"{rows[0]['Supplier']} — {inr_short(rows[0]['Outstanding'])}"),
                 ("Over 30 days", inr(overdue))],
                ["Supplier", "Bills", "Outstanding", "Oldest (days)"], rows,
                "Outstanding = the bill less what has been paid and what went back on debit notes.",
                {"tab": "payments"})


def a_pending_grns(db, read, sc, per):
    pend = cc.pending_grns(db, sc["wh_ids"])
    title = "Pending GRNs"
    line = (f"{title}: {plural(pend['count'], 'draft GRN')} worth {inr_short(pend['value'])} not yet "
            f"posted, and {plural(pend['documents'], 'invoice')} waiting for review.")
    rows = [{"GRN": p.grn_no or f"#{p.id}", "Supplier": p.supplier.name if p.supplier else "—",
             "Warehouse": p.warehouse.name if p.warehouse else "—", "Invoice": p.invoice_number,
             "Value": p.grand_total, "Raised": _local_time(p.created_at)} for p in pend["rows"]]
    return _out(title, fmt_qty(pend["count"]), line, None,
                [("Draft value", inr(pend["value"])), ("Invoices to review", fmt_qty(pend["documents"]))],
                ["GRN", "Supplier", "Warehouse", "Invoice", "Value", "Raised"], rows, None,
                {"tab": "purchases"})


def a_returns(db, read, sc, per):
    t = pos_insights.totals(per["start"], per["end"], sc["at"]) if pos_insights.available() else {}
    dn = cc.debit_notes_between(db, per["start"], per["end"], sc["wh_ids"])
    store_v = float(t.get("returns") or 0)
    total = round(store_v + dn["value"], 2)
    title = _titled(per["label"], "Returns")
    line = (f"{title}: {inr(total)} — {plural(t.get('return_notes', 0), 'customer return')} "
            f"({inr(store_v)}) and {plural(dn['count'], 'debit note')} to suppliers ({inr(dn['value'])}).")
    rows = [{"Kind": "Debit note", "Number": r.code, "Party": r.supplier.name if r.supplier else "—",
             "Value": r.total, "When": _local_time(r.posted_at)} for r in dn["rows"]]
    if t.get("return_notes"):
        rows.insert(0, {"Kind": "Customer returns", "Number": f"{t['return_notes']} credit note(s)",
                        "Party": "Stores", "Value": store_v, "When": per["label"]})
    return _out(title, inr(total), line, None,
                [("Customer returns", inr(store_v)), ("Debit notes", inr(dn["value"]))],
                ["Kind", "Number", "Party", "Value", "When"], rows, None, {"tab": "returns"})


def _match_words(words, *fields):
    hay = " ".join(_norm(f) for f in fields if f)
    return all(pos_insights._stem(w) in hay or w in hay for w in words)


def a_stock(db, read, sc, per):
    item = (read.get("item") or "").strip()
    words = pos_insights.words_of(item)
    if sc["floor_ids"] is not None:
        s = pos_insights.stock(sc["floor_ids"], item or None)
        if not s.get("available"):
            return _no_pos()
        title = f"Stock on {sc['place'] or read.get('floor')}"
        line = (f"{title}: {plural(s['units'], 'piece')} of {plural(s['items'], 'product')} "
                f"worth {inr_short(s['value'])} at cost.")
        return _out(title, fmt_qty(s["units"]), line,
                    f"{title}: {fmt_qty(s['units'])} pieces worth {spoken_inr(s['value'])}.",
                    [("Products", fmt_qty(s["items"])), ("Value at cost", inr(s["value"]))],
                    ["SKU", "Product", "Category", "Floor", "Qty", "Value"],
                    [{"SKU": r["sku"], "Product": r["product"], "Category": r["category"],
                      "Floor": r["floor"] or "—", "Qty": r["qty"], "Value": r["value"]}
                     for r in s["rows"][:200]],
                    "A store's stock is one figure for the whole shop; the floor is where the pieces are kept.")

    if sc["store"]:
        s = pos_insights.stock(None, item or None)
        if not s.get("available"):
            return _no_pos()
        title = "Stock in the stores" + (f" — {item.title()}" if item else "")
        line = (f"{title}: {plural(s['units'], 'piece')} of {plural(s['items'], 'product')} "
                f"worth {inr_short(s['value'])} at cost.")
        return _out(title, fmt_qty(s["units"]), line, None,
                    [("Products", fmt_qty(s["items"])), ("Value at cost", inr(s["value"]))],
                    ["SKU", "Product", "Category", "Floor", "Qty", "Value"],
                    [{"SKU": r["sku"], "Product": r["product"], "Category": r["category"],
                      "Floor": r["floor"] or "—", "Qty": r["qty"], "Value": r["value"]}
                     for r in s["rows"][:200]],
                    f"The till keeps one stock figure for every branch it serves, so this is not "
                    f"{sc['store']['name']} alone — ask by floor to narrow it.")

    q = db.query(models.StockBalance).filter(models.StockBalance.qty > stock_loc.TOLERANCE)
    if sc["wh_ids"]:
        q = q.filter(models.StockBalance.warehouse_id.in_(sc["wh_ids"]))
    per_product, whs = {}, set()
    for b in q.all():
        p = b.product
        if not p or (words and not _match_words(words, p.description, p.category,
                                                p.category_section, p.sku, p.brand)):
            continue
        r = per_product.setdefault(p.id, {"SKU": p.sku, "Product": p.description,
                                          "Category": p.category or "", "Qty": 0.0, "Value": 0.0})
        r["Qty"] = round(r["Qty"] + float(b.qty or 0), 3)
        r["Value"] = round(r["Value"] + b.value, 2)
        whs.add(b.warehouse_id)
    rows = sorted(per_product.values(), key=lambda r: -r["Value"])
    wq, wv = round(sum(r["Qty"] for r in rows), 3), round(sum(r["Value"] for r in rows), 2)

    if sc["warehouse"]:
        title = f"Stock at {sc['warehouse']['name']}" + (f" — {item.title()}" if item else "")
        line = f"{title}: {plural(wq, 'unit')} of {plural(len(rows), 'product')} worth {inr_short(wv)}."
        facts = [("Products", fmt_qty(len(rows))), ("Value", inr(wv))]
    else:
        s = pos_insights.stock(None, item or None) if pos_insights.available() else {}
        title = "Stock Available" + (f" — {item.title()}" if item else "")
        line = (f"{title}: {plural(wq, 'unit')} worth {inr_short(wv)} in {plural(len(whs), 'warehouse')}"
                + (f", plus {plural(s['units'], 'unit')} worth {inr_short(s['value'])} in the stores."
                   if s.get("available") else "."))
        facts = [("In warehouses", f"{fmt_qty(wq)} units · {inr_short(wv)}"),
                 ("In stores", f"{fmt_qty(s['units'])} units · {inr_short(s['value'])}" if s.get("available") else None),
                 ("Products", fmt_qty(len(rows)))]
    return _out(title, fmt_qty(wq), line, None, facts,
                ["SKU", "Product", "Category", "Qty", "Value"], rows[:200],
                "Warehouse stock is valued at each warehouse's own average cost.",
                {"tab": "inventory"})


def a_stock_rank(db, read, sc, per):
    bottom = read.get("order") == "bottom"
    if read.get("group_by") == "category":
        agg = {}
        q = db.query(models.StockBalance).filter(models.StockBalance.qty > stock_loc.TOLERANCE)
        if sc["wh_ids"]:
            q = q.filter(models.StockBalance.warehouse_id.in_(sc["wh_ids"]))
        for b in q.all():
            cat = (b.product.category if b.product else None) or "(uncategorised)"
            a = agg.setdefault(cat, {"label": cat, "value": 0.0, "qty": 0.0})
            a["value"] = round(a["value"] + b.value, 2)
            a["qty"] = round(a["qty"] + float(b.qty or 0), 3)
        rows, noun = sorted(agg.values(), key=lambda r: -r["value"]), "Category"
    else:
        rows = [{"label": r["name"], "value": r["value"], "qty": r["qty"]}
                for r in stock_loc.warehouse_totals(db, warehouse_ids=sc["wh_ids"])]
        rows.sort(key=lambda r: -r["value"])
        noun = "Warehouse"
    if not rows:
        return _out(f"Stock Value by {noun}", "—", "No stock is recorded anywhere yet.")
    pick = rows[-1] if bottom else rows[0]
    word = "Lowest" if bottom else "Highest"
    line = f"{word} Stock Value: {pick['label']} — {inr_short(pick['value'])}."
    total = sum(r["value"] for r in rows) or 1
    return _out(f"{word} Stock Value by {noun}", inr_short(pick["value"]), line,
                f"{word} stock value: {pick['label']}, {spoken_inr(pick['value'])}.",
                [("Share", f"{pick['value'] / total * 100:.1f}%"), ("Units", fmt_qty(pick["qty"]))],
                [noun, "Units", "Value", "Share %"],
                [{noun: r["label"], "Units": r["qty"], "Value": r["value"],
                  "Share %": round(r["value"] / total * 100, 1)} for r in rows],
                None, {"tab": "central"})


def a_low_stock(db, read, sc, per):
    low = pos_insights.low_stock(limit=500, item=read.get("item") or None)
    if not low.get("available"):
        return _no_pos()
    title = "Low Stock"
    rows = low["rows"]
    line = (f"{title}: {plural(low['count'], 'product')} at or below reorder level in the stores"
            + (f" — emptiest is {rows[0]['product']} with {fmt_qty(rows[0]['stock'])} left." if rows else "."))
    return _out(title, fmt_qty(low["count"]), line,
                f"Low stock: {plural(low['count'], 'product')} at or below reorder level.",
                [("Out of stock", fmt_qty(sum(1 for r in rows if r["stock"] <= 0)))],
                ["SKU", "Product", "Category", "Floor", "Stock", "Reorder level"],
                [{"SKU": r["sku"], "Product": r["product"], "Category": r["category"],
                  "Floor": r["floor"] or "—", "Stock": r["stock"],
                  "Reorder level": r["reorder_level"]} for r in rows],
                "Only the stores keep reorder levels — warehouse stock has none, so it is not part of this.")


def a_dead_stock(db, read, sc, per):
    days = int(read["days"]) if str(read.get("days") or "").isdigit() else None
    d = cc.dead_counts(db, days)
    words = pos_insights.words_of(read.get("item") or "")
    rows = [r for r in d["rows"] if not words or _match_words(words, r["name"], r["category"], r["sku"])]
    value = round(sum(r["stock_value"] for r in rows), 2)
    title = f"Dead Stock ({d['days']}+ days)"
    line = (f"Dead Stock: {plural(len(rows), 'product')} worth {inr_short(value)} with no sale or "
            f"dispatch for {d['days']}+ days.")
    rows.sort(key=lambda r: -r["stock_value"])
    return _out(title, fmt_qty(len(rows)), line,
                f"Dead stock: {plural(len(rows), 'product')} worth {spoken_inr(value)}.",
                [("Value at cost", inr(value)), ("Idle 180+ days", fmt_qty(sum(1 for r in rows if r['days_idle'] >= 180)))],
                ["SKU", "Product", "Category", "Qty", "Value", "Idle days", "Last sold"],
                [{"SKU": r["sku"], "Product": r["name"], "Category": r["category"], "Qty": r["qty"],
                  "Value": r["stock_value"], "Idle days": r["days_idle"],
                  "Last sold": r["last_sold"] or "never"} for r in rows[:500]],
                "Idle means no till sale and no dispatch since the date shown. Company-wide.",
                {"tab": "deadstock"})


def a_transfers(db, read, sc, per):
    q = db.query(models.StockOutward).filter(models.StockOutward.status == "posted")
    if sc["wh_ids"]:
        q = q.filter(models.StockOutward.from_warehouse_id.in_(sc["wh_ids"])
                     | models.StockOutward.to_warehouse_id.in_(sc["wh_ids"]))
    transit = [o for o in q.all() if o.to_warehouse_id]
    sent = db.query(models.StockOutward).filter(models.StockOutward.posted_at >= per["start"],
                                                models.StockOutward.posted_at < per["end"])
    if sc["wh_ids"]:
        sent = sent.filter(models.StockOutward.from_warehouse_id.in_(sc["wh_ids"]))
    sent = sent.all()
    t_units = round(sum(o.total_qty for o in transit), 3)
    s_units = round(sum(o.total_qty for o in sent), 3)
    title = "Transfers"
    line = (f"In Transit: {plural(t_units, 'unit')} on {plural(len(transit), 'transfer')}; "
            f"{_phrase(per['label'])} {plural(len(sent), 'dispatch', 'dispatches')} went out ({fmt_qty(s_units)} units).")
    rows = [{"Code": o.code, "From": o.from_warehouse.name if o.from_warehouse else o.from_location,
             "To": o.to_destination, "Units": o.total_qty,
             "Status": "in transit" if o in transit else o.status,
             "Dispatched": _local_time(o.posted_at)} for o in (transit + [x for x in sent if x not in transit])]
    return _out(title, fmt_qty(t_units), line, None,
                [("Sent " + _phrase(per["label"]), fmt_qty(s_units))],
                ["Code", "From", "To", "Units", "Status", "Dispatched"], rows, None,
                {"tab": "outward"})


def a_counts(db, read, sc, per, role):
    from .users import rank, ROLE_RANK
    c = cc.counts(db, sc["wh_ids"], with_users=rank(role) >= ROLE_RANK["superadmin"])
    line = (f"{plural(c['warehouses'], 'warehouse')}, {plural(c['stores'], 'store')} and "
            f"{plural(c['counters'], 'POS counter')}"
            + (f", with {plural(c['users'], 'active user')}." if "users" in c else "."))
    return _out("Locations & People", fmt_qty(c["warehouses"]), line, None,
                [(k.title(), fmt_qty(v)) for k, v in c.items()], open_={"tab": "locations"})


def a_activity(db, read, sc, per, role, allowed):
    from .users import rank, ROLE_RANK
    who = None
    if read.get("person"):
        users = [{"name": u.full_name or u.username, "username": u.username}
                 for u in db.query(models.User).all()]
        who = _best(read["person"], users) or _best(read["person"], users, key="username")
        if who is None and pos_insights.available() and pos_insights.people_named(read["person"]):
            # Not a user here, but somebody at the tills — what they did is their bills.
            sub = {**read, "person_role": ""}
            if _who(db, sub, sc):
                return a_bills(db, sub, sc, per)
    hide = () if rank(role) >= ROLE_RANK["superadmin"] else ("users", "settings")
    f = audit_svc.feed(db, limit=200, username=who["username"] if who else None,
                       date_from=per["from"], date_to=per["to"],
                       allowed_warehouses=allowed, hide_screens=hide)
    ev = f["events"]
    people = {e["username"] for e in ev if e["username"]}
    title = _titled(per["label"], "Activity") + (f" — {who['name']}" if who else "")
    if not ev:
        return _out(title, "0", f"Nothing was recorded on the audit trail {_phrase(per['label'])}"
                    + (f" for {who['name']}." if who else "."), open_={"tab": "audit"})
    last = ev[0]
    line = (f"{title}: {plural(len(ev), 'action')} by {plural(len(people), 'person', 'people')} — "
            f"latest, {last['who']} {last['summary']} at {(last['local'] or '')[11:16]}.")
    return _out(title, fmt_qty(len(ev)), line, None,
                [("Refused attempts", fmt_qty(sum(1 for e in ev if e["outcome"] == "refused")))],
                ["Time", "Who", "Module", "What", "Where"],
                [{"Time": (e["local"] or "").replace("T", " "), "Who": e["who"], "Module": e["module"],
                  "What": e["summary"], "Where": e["warehouse"] or "—"} for e in ev],
                None, {"tab": "audit"})


# ---------------------------------------------------------------------------
#  One person's bills — who billed, who sold, who bought
# ---------------------------------------------------------------------------
_ROLE_WORDS = {"cashier": "billed by", "salesperson": "sold by", "customer": "for", "": "by"}


def _who(db, read, sc):
    """Narrow the bills to the person the question names. Sets sc["who"] and
    returns it; None when no person was named; False when nobody at the tills
    — staff or customer — is called that."""
    name = (read.get("person") or "").strip()
    role = read.get("person_role") or ""
    if not name or not pos_insights.available():
        return None
    at = dict(sc.get("at") or {})
    hit = _resolve(name, role)
    if hit is None:
        return False
    kind, found, role = hit
    names = ", ".join(h["name"] for h in found[:3]) + (" …" if len(found) > 3 else "")
    if kind == "staff":
        at[{"cashier": "cashier_ids", "salesperson": "staff_ids"}.get(role, "person_ids")] = \
            [p["id"] for p in found]
    else:
        at["customer_ids"] = [h["id"] for h in found]
    sc["at"] = at
    sc["who"] = {"kind": kind, "name": names, "role": role,
                 "label": f"{_ROLE_WORDS[role]} {names}"}
    return sc["who"]


def _who_words(sc):
    return f" {sc['who']['label']}" if sc.get("who") else ""


def _nobody(name):
    return _out("Who?", "—", f"Nobody called “{name}” bills, sells or buys at the stores.", ok=False)


def a_bills(db, read, sc, per):
    if not pos_insights.available():
        return _no_pos()
    who = sc.get("who")
    t = pos_insights.totals(per["start"], per["end"], sc["at"])
    rows = pos_insights.bills(per["start"], per["end"], sc["at"])
    title = _titled(per["label"], "Bills" + _who_words(sc))
    if sc["place"]:
        title += f" at {sc['place']}"
    cancelled = sum(1 for b in rows if b["status"] == "cancelled")
    if not t["bills"]:
        line = f"{title}: none."
        if who:
            last = pos_insights.bills(per["start"].replace(year=2000), per["end"], sc["at"], limit=1)
            if last:
                line = (f"{title}: none. The last was {last[0]['bill_no']} on "
                        f"{_local_time(last[0]['at'])} for {inr(last[0]['total'])}.")
        return _out(title, "0", line, None, [("Cancelled", str(cancelled) if cancelled else None)],
                    *_bill_table(rows, who), None, {"tab": "central"})
    live = [b for b in rows if b["status"] != "cancelled"]
    last = live[0] if live else None
    line = (f"{title}: {plural(t['bills'], 'bill')} worth {inr(t['gross'])}"
            + (f", {cancelled} cancelled" if cancelled else "")
            + (f" — the latest {last['bill_no']} at {_local_time(last['at'])[11:]} for {inr(last['total'])}"
               + (f" to {last['customer']}" if last["customer"] != "Walk-in" and (not who or who["kind"] != "customer") else "")
               if last else "") + ".")
    buyers = {b["phone"] or b["customer"] for b in live if b["customer"] != "Walk-in"}
    facts = [("Bills", fmt_qty(t["bills"])), ("Billed", inr(t["gross"])),
             ("Average bill", inr(round(t["gross"] / t["bills"], 2))),
             ("Discount given", inr(t["discount"]) if t["discount"] else None),
             ("Returned", inr(t["returns"]) if t["returns"] else None),
             ("Customers", str(len(buyers)) if who is None or who["kind"] != "customer" else None),
             ("Cancelled", str(cancelled) if cancelled else None)]
    note = None
    if len(rows) >= 300:
        note = "The latest 300 bills are listed; the figures above count them all."
    if who and who["kind"] == "staff" and not who["role"]:
        note = ((note + " ") if note else "") + \
            f"Bills {who['name']} billed at the counter or sold on the floor."
    return _out(title, inr(t["gross"]), line,
                f"{title}: {plural(t['bills'], 'bill')}, {spoken_inr(t['gross'])}.",
                facts, *_bill_table(rows, who), note, {"tab": "central"})


def _bill_table(rows, who):
    cols = ["Bill", "When", "Store", "Floor", "Counter", "Billed by", "Salesperson",
            "Customer", "Mobile", "Payment", "Discount", "Total", "Status"]
    return cols, [{"Bill": b["bill_no"], "When": _local_time(b["at"]), "Store": b["branch"],
                   "Floor": b["floor"], "Counter": b["counter"], "Billed by": b["cashier"],
                   "Salesperson": b["staff"], "Customer": b["customer"], "Mobile": b["phone"] or "—",
                   "Payment": b["method"], "Discount": b["discount"], "Total": b["total"],
                   "Status": b["status"]} for b in rows]


def a_supplier(db, sup, per):
    """One supplier: what is owed them and what came in from them."""
    owed = [b for b in cc.outstanding(db) if b["supplier"] == sup.name]
    total = round(sum(b["outstanding"] for b in owed), 2)
    grns = (db.query(models.Purchase)
              .filter(models.Purchase.supplier_id == sup.id, models.Purchase.status == "posted",
                      models.Purchase.posted_at >= per["start"], models.Purchase.posted_at < per["end"])
              .order_by(models.Purchase.posted_at.desc()).all())
    value = round(sum(float(p.grand_total or 0) for p in grns), 2)
    line = (f"{sup.name}: " + (f"{inr(total)} owed on {plural(len(owed), 'bill')}" if owed else "nothing owed")
            + f"; {plural(len(grns), 'GRN')} worth {inr(value)} {_phrase(per['label'])}.")
    return _out(sup.name, inr(total), line, None,
                [("Outstanding", inr(total)), ("Oldest unpaid (days)",
                  str(max((b["days"] or 0) for b in owed)) if owed else None),
                 (_titled(per["label"], "GRNs"), f"{len(grns)} · {inr(value)}"),
                 ("GSTIN", sup.gstin), ("Phone", sup.phone)],
                ["Invoice date", "Bill", "Outstanding", "Days"],
                [{"Invoice date": b["purchase"].invoice_date or "—", "Bill": b["purchase"].grand_total,
                  "Outstanding": b["outstanding"], "Days": b["days"]} for b in owed],
                "Supplier payments are company-wide.", {"tab": "payments"})


def _warehouse_products(db, name, limit=200):
    """Warehouse items `name` means: a whole SKU or barcode, else every word in
    the description, category, SKU or brand."""
    code = name.strip().lower()
    if code and " " not in code:
        one = db.query(models.Product).filter((func.lower(models.Product.sku) == code)
                                              | (func.lower(models.Product.barcode) == code)).limit(5).all()
        if one:
            return one
    words = pos_insights.words_of(name)
    if not words:
        return []
    q = db.query(models.Product)
    for w in words:
        like = f"%{pos_insights._stem(w)}%"
        q = q.filter((models.Product.description.ilike(like)) | (models.Product.category.ilike(like))
                     | (models.Product.sku.ilike(like)) | (models.Product.brand.ilike(like)))
    return q.order_by(models.Product.description).limit(limit).all()


def _held(db, product_ids, wh_ids):
    """{warehouse product id: units held} in the warehouses an account sees."""
    if not product_ids:
        return {}
    q = (db.query(models.StockBalance.product_id, func.sum(models.StockBalance.qty))
           .filter(models.StockBalance.product_id.in_(product_ids)))
    if wh_ids:
        q = q.filter(models.StockBalance.warehouse_id.in_(wh_ids))
    return {pid: round(float(n or 0), 3) for pid, n in q.group_by(models.StockBalance.product_id).all()}


def a_product(db, name, sc, per, store_rows=None, wh_rows=None):
    """A product by what somebody called it: what it costs, where it is, what sold.
    One match reads as that product; several as a list of them."""
    store = store_rows if store_rows is not None else pos_insights.products_named(name)
    whp = wh_rows if wh_rows is not None else _warehouse_products(db, name)
    # the same item in the warehouse — linked by id, or sharing the SKU — is one row, not two
    by_sku = {p.sku: p.id for p in whp if p.sku}
    wid = {s["id"]: s["warehouse_product_id"] or by_sku.get(s["sku"]) for s in store}
    linked = {w for w in wid.values() if w}
    whp = [p for p in whp if p.id not in linked]
    held = _held(db, list(linked | {p.id for p in whp}), sc["wh_ids"])
    sold = pos_insights.sold_by_product(per["start"], per["end"], [s["id"] for s in store], sc["at"])
    sold_label = _titled(per["label"], "Sold")
    rows = []
    for s in store:
        b, units, amount = sold.get(s["id"], (0, 0, 0))
        rows.append({"SKU": s["sku"], "Product": s["name"], "Category": s["category"], "Price": s["price"],
                     "In stores": s["qty"], "Floor": s["floor"] or "—",
                     "In warehouses": held.get(wid[s["id"]], 0), sold_label: units, "Sales": amount})
    for p in whp:
        rows.append({"SKU": p.sku, "Product": p.description, "Category": p.category or "", "Price": p.mrp or 0,
                     "In stores": 0, "Floor": "—", "In warehouses": held.get(p.id, 0), sold_label: 0, "Sales": 0})
    if not rows:
        return None
    cols = ["SKU", "Product", "Category", "Price", "In stores", "Floor", "In warehouses", sold_label, "Sales"]
    in_st = round(sum(r["In stores"] for r in rows), 3)
    in_wh = round(sum(r["In warehouses"] for r in rows), 3)
    units = round(sum(r[sold_label] for r in rows), 3)
    sales = round(sum(r["Sales"] for r in rows), 2)
    when = _phrase(per["label"])
    if len(rows) == 1:
        r = rows[0]
        s = store[0] if store else None
        where = f" on {r['Floor']}" if r["Floor"] != "—" else ""
        line = (f"{r['Product']} ({r['SKU']}): {fmt_qty(r['In stores'])} in the stores{where}"
                + (f", {fmt_qty(r['In warehouses'])} in the warehouses" if r["In warehouses"] else "")
                + (f"; {inr(r['Price'])} each" if r["Price"] else "")
                + f"; {fmt_qty(units)} sold {when}" + (f" for {inr(sales)}." if sales else "."))
        facts = [("SKU", r["SKU"]), ("Barcode", s and s["barcode"]), ("Category", r["Category"] or None),
                 ("Selling price", inr(r["Price"]) if r["Price"] else None),
                 ("MRP", inr(s["mrp"]) if s and s["mrp"] else None), ("Cost", inr(s["cost"]) if s and s["cost"] else None),
                 ("In the stores", fmt_qty(r["In stores"])), ("Floor", s and s["floor"]),
                 ("In the warehouses", fmt_qty(r["In warehouses"]) if r["In warehouses"] else None),
                 (_titled(per["label"], "Sold"), f"{fmt_qty(units)} · {inr(sales)}"),
                 ("Supplier", s and s["supplier"]), ("Brand", s and s["brand"]), ("Bought on", s and s["purchased"]),
                 ("Reorder at", fmt_qty(s["reorder"]) if s and s["reorder"] else None)]
        note = None
        if s and r["In stores"] <= 0:
            note = "Out of stock in the stores."
        elif s and s["reorder"] and r["In stores"] <= s["reorder"]:
            note = "At or below its reorder level."
        return _out(r["Product"], fmt_qty(r["In stores"] + r["In warehouses"]), line, None, facts, cols, rows,
                    note, {"tab": "inventory"})
    title = f"Products — “{name}”"
    line = (f"{plural(len(rows), 'product')} match “{name}”: {fmt_qty(in_st)} pieces in the stores"
            + (f", {fmt_qty(in_wh)} in the warehouses" if in_wh else "")
            + f"; {fmt_qty(units)} sold {when}" + (f" for {inr(sales)}." if sales else "."))
    rows.sort(key=lambda x: (-x["Sales"], -(x["In stores"] + x["In warehouses"])))
    return _out(title, str(len(rows)), line, None,
                [("Products", str(len(rows))), ("In the stores", fmt_qty(in_st)), ("In the warehouses", fmt_qty(in_wh)),
                 (_titled(per["label"], "Sold"), f"{fmt_qty(units)} · {inr(sales)}")],
                cols, rows[:200], None, {"tab": "inventory"})


def a_store_supplier(db, name, products, sc, per):
    """A supplier known only from the store's products — what is on the shelves
    from them, and how it sells."""
    sold = pos_insights.sold_by_product(per["start"], per["end"], [p["id"] for p in products], sc["at"])
    who = sorted({p["supplier"] for p in products})
    units = round(sum(sold.get(p["id"], (0, 0, 0))[1] for p in products), 3)
    sales = round(sum(sold.get(p["id"], (0, 0, 0))[2] for p in products), 2)
    stock = round(sum(p["qty"] for p in products), 3)
    value = round(sum(p["qty"] * p["cost"] for p in products), 2)
    title = who[0] if len(who) == 1 else f"Supplier — “{name}”"
    line = (f"{title}: {plural(len(products), 'product')} in the stores, {fmt_qty(stock)} pieces worth "
            f"{inr_short(value)} at cost; {fmt_qty(units)} sold {_phrase(per['label'])} for {inr(sales)}.")
    sold_label = _titled(per["label"], "Sold")
    rows = [{"SKU": p["sku"], "Product": p["name"], "Supplier": p["supplier"], "Bought on": p["purchased"] or "—",
             "In stores": p["qty"], sold_label: sold.get(p["id"], (0, 0, 0))[1], "Sales": sold.get(p["id"], (0, 0, 0))[2]}
            for p in products]
    rows.sort(key=lambda r: -r["Sales"])
    return _out(title, inr_short(sales), line, None,
                [("Products", str(len(products))), ("In stock", f"{fmt_qty(stock)} · {inr_short(value)}"),
                 (sold_label, f"{fmt_qty(units)} · {inr(sales)}")],
                ["SKU", "Product", "Supplier", "Bought on", "In stores", sold_label, "Sales"], rows[:200],
                "Taken from the supplier written on each store product.", {"tab": "inventory"})


def _starts_words(name, *fields):
    """Every word of `name` starts a word of the fields — "ams" is AMS Garments,
    "ram" is not Sriram."""
    want = re.findall(r"[a-z0-9]+", (name or "").lower())
    have = re.findall(r"[a-z0-9]+", " ".join(str(f or "") for f in fields).lower())
    return bool(want) and all(any(h.startswith(w) for h in have) for w in want)


_LOOKUP_FILLER = re.compile(
    r"\b(?:show|me|give|get|details?|info|information|about|who|is|was|find|search|for|tell|"
    r"the|of|what|on|please|look\s*up|lookup|profile|everything|anything|all|any|today'?s?|"
    r"yesterday'?s?|this|last|week|month|year|an?|and|do|we|have|named|called|name|"
    r"history|summary|report|status)\b")
_HINTS = (("customer", ("customer", "client", "buyer")),
          ("staff", ("employee", "staff", "cashier", "salesman", "salesperson", "biller", "worker", "user")),
          ("supplier", ("supplier", "vendor", "party")),
          ("place", ("store", "shop", "branch", "warehouse", "godown")),
          ("product", ("product", "item", "design", "sku", "garment")))


def _lookup(db, question, read, role, allowed, hr=False):
    """A bare name, looked for everywhere in the software: someone who bills or
    sells at the tills, a customer, a user here, a supplier, a store or warehouse,
    a product. The first that matches answers; the rest are named beneath it.
    Returns (intent, answer) or None."""
    t = f" {question.lower()} "
    hint = next((k for k, ws in _HINTS if re.search(r"\b(?:" + "|".join(ws) + r")s?\b", t)), None)
    name = (read.get("person") or read.get("item") or "").strip().lower()
    if not name:
        name = re.sub(r"[^a-z0-9.' -]", " ", t)
        for _, ws in _HINTS:
            name = re.sub(r"\b(?:" + "|".join(ws) + r")s?\b", " ", name)
        name = " ".join(_LOOKUP_FILLER.sub(" ", name).split()).strip(" .'-")
    # a product's full name runs longer than a person's — "Kanchipuram Silk Saree - Red"
    if len(name) < 3 or len(re.findall(r"[a-z0-9]+", name)) > 6:
        return None
    try:
        base = _scope(db, {}, allowed)
    except Refused:
        return None
    per = _period(read, "today")
    found = []                       # (kind, what it is, intent, answer(), whole name?)

    if hr:
        emps = hr_svc.find_employees(db, name)
        if emps:
            found.append(("staff", "employee: " + ", ".join(e.name for e in emps[:3]), "employee",
                          lambda: a_employee(db, emps[0]),
                          any(name in (_norm(e.name), _norm(e.code)) for e in emps)))

    if pos_insights.available():
        staff = pos_insights.people_named(name)
        if staff:
            def staff_answer():
                sc = dict(base)
                sub = {"person": name, "person_role": ""}
                return a_bills(db, sub, sc, per) if _who(db, sub, sc) else None
            found.append(("staff", "at the tills: " + ", ".join(p["name"] for p in staff[:3]),
                          "bills", staff_answer,
                          any(name in (_norm(p["name"]), _norm(p["username"])) for p in staff)))
    if crm.available():
        one = crm.find_customer(name)
        many = [] if one else crm.find_customers_named(name)
        if one or many:
            found.append(("customer", "customer: " + (one["name"] if one else
                          ", ".join(h["name"] for h in many[:3])),
                          "customer", lambda: a_customer(db, {"person": name, "question": question},
                                                         base, per), bool(one)))
    at_tills = {_norm(p["name"]) for p in staff} if pos_insights.available() else set()
    at_tills |= {_norm(e.name) for e in (emps if hr else [])}
    users = [u for u in db.query(models.User).all() if _starts_words(name, u.full_name, u.username)
             and _norm(u.full_name or u.username) not in at_tills]   # the same person, signed in to both
    if users:
        found.append(("staff", "user here: " + ", ".join(u.full_name or u.username for u in users[:3]),
                      "activity", lambda: a_activity(db, {"person": name}, base, per, role, allowed),
                      any(name in (_norm(u.full_name), _norm(u.username)) for u in users)))
    sups = [s for s in db.query(models.Supplier).all()
            if _starts_words(name, s.name) or any(_starts_words(name, a) for a in (s.aliases or []))]
    per_m = _period(read, "month")
    if sups:
        found.append(("supplier", "supplier: " + ", ".join(s.name for s in sups[:3]),
                      "pending_payments", lambda: a_supplier(db, sups[0], per_m),
                      any(_norm(x.name) == name for x in sups)))
    else:
        # a supplier the warehouse has never billed, known from the store's products
        theirs = [p for p in (pos_insights.products_from(name) if pos_insights.available() else [])
                  if _starts_words(name, p["supplier"])]
        if theirs:
            found.append(("supplier", "supplier: " + ", ".join(sorted({p["supplier"] for p in theirs})[:3]),
                          "stock", lambda: a_store_supplier(db, name, theirs, base, per_m),
                          any(_norm(p["supplier"]) == name for p in theirs)))
    for kind, rows in (("store", _store_rows(db)), ("warehouse", _warehouse_rows(db))):
        hit = next((r for r in rows if _starts_words(name, r["name"]) or _norm(r.get("code")) == name), None)
        if hit:
            def place_answer(kind=kind, hit=hit):
                try:
                    sc = _scope(db, {kind: hit["name"]}, allowed)
                except Refused as r:
                    return _out("Not allotted", "—", str(r), ok=False)
                return a_sales(db, {}, sc, per)
            found.append(("place", f"{kind}: {hit['name']}", "sales", place_answer,
                          name in (_norm(hit["name"]), _norm(hit.get("code")))))
    in_store = pos_insights.products_named(name) if pos_insights.available() else []
    in_wh = _warehouse_products(db, name)
    if in_store or in_wh:
        one = (in_store[0]["name"] if in_store else in_wh[0].description) if len(in_store) + len(in_wh) == 1 else None
        whole = (any(name in (_norm(p["name"]), _norm(p["sku"]), _norm(p["barcode"])) for p in in_store)
                 or any(name in (_norm(p.description), _norm(p.sku), _norm(p.barcode)) for p in in_wh))
        found.append(("product", f"product: {one}" if one else f"products: “{name}”", "stock",
                      lambda: a_product(db, name, base, per_m, in_store, in_wh), whole))
    if not found:
        return None
    # What the question said it was comes first, then a whole name over a part of one
    # (and the HR record before the same person's bills, being the fuller answer).
    found.sort(key=lambda f: (bool(hint) and f[0] != hint, not f[4]))
    for i, (kind, what, intent, answer, _whole) in enumerate(found):
        out = answer()
        if not out or not out.get("ok", True):
            continue
        others = [f[1] for j, f in enumerate(found) if j != i]
        if others:
            out["note"] = ((out.get("note") or "") + " Also matches — " + " · ".join(others) + ".").strip()
        return intent, out
    return None


# ---------------------------------------------------------------------------
#  HR — payroll, attendance, leave, headcount, one employee
# ---------------------------------------------------------------------------
def a_payroll(db, read, sc, per):
    month = str(read["date_from"])[:7] if read.get("date_from") else None
    runs = hr_svc.list_runs(db)
    run = next((r for r in runs if r["month"] == month), None) if month else (runs[0] if runs else None)
    if run is None:
        label = hr_svc.month_label(month) if month else "Any month"
        return _out("Payroll", "—", f"{label}'s payroll has not been worked out yet — HR → Payroll.",
                    open_={"tab": "hr_payroll"}, ok=bool(month))
    full = hr_svc.get_run(db, run["month"])
    t = full["totals"]
    title = f"Payroll — {full['label']}"
    line = (f"{title} ({full['status']}): {inr(t.get('net', 0))} net to {plural(t.get('employees', 0), 'person', 'people')}; "
            f"gross {inr(t.get('gross', 0))}, cost to company {inr(t.get('cost', 0))}.")
    facts = [("Status", full["status"]), ("Gross", inr(t.get("gross", 0))), ("Net", inr(t.get("net", 0))),
             ("PF (both shares)", inr(t.get("pf", 0) + t.get("employer_pf", 0))),
             ("ESI (both shares)", inr(t.get("esi", 0) + t.get("employer_esi", 0))),
             ("TDS", inr(t.get("tds", 0))), ("Professional Tax", inr(t.get("pt", 0))),
             ("Advances recovered", inr(t.get("advance", 0)) if t.get("advance") else None),
             ("Paid on", full.get("paid_on"))]
    rows = [{"Employee": x["name"], "Code": x["code"], "Paid days": x["paid_days"], "Gross": x["gross"],
             "Deductions": x["total_deductions"], "Net": x["net"]} for x in full["slips"]]
    return _out(title, inr(t.get("net", 0)), line, f"{title}: {spoken_inr(t.get('net', 0))} net.", facts,
                ["Employee", "Code", "Paid days", "Gross", "Deductions", "Net"], rows, None, {"tab": "hr_payroll"})


def a_attendance(db, read, sc, per):
    d = hr_svc.day_summary(db, per["from"])
    c = d["counts"]
    present = c.get("P", 0) + c.get("OD", 0) + c.get("HD", 0)
    off = c.get("WO", 0) + c.get("H", 0)
    title = _titled(per["label"], "Attendance")
    if not d["total"]:
        return _out(title, "—", f"{title}: nobody is employed yet — HR → Employees.", open_={"tab": "hr_employees"})
    t = f" {(read.get('question') or '').lower()} "
    line = (f"{title}: {present} of {d['total'] - off} present, {plural(c.get('L', 0), 'person', 'people')} on leave, "
            f"{c.get('A', 0)} absent, {c.get('', 0)} not marked" + (f"; {len(d['late'])} came in late." if d["late"] else "."))
    rows = d["rows"]
    if _has(t, ("absent",)):
        rows = [r for r in rows if r["_s"] in ("A", "")]
    elif _has(t, ("late",)):
        rows = d["late"]
    return _out(title, f"{present}/{d['total'] - off}", line, None,
                [("Present", str(present)), ("On leave", str(c.get("L", 0))), ("Absent", str(c.get("A", 0))),
                 ("Not marked", str(c.get("", 0))), ("Weekly off / holiday", str(off) if off else None),
                 ("Late", str(len(d["late"])) if d["late"] else None)],
                ["Employee", "Code", "Department", "Status", "In", "Out", "Late (min)"],
                [{k: v for k, v in r.items() if k != "_s"} for r in rows], None, {"tab": "hr_attendance"})


def a_staff_leave(db, read, sc, per):
    on = hr_svc.list_leave(db, status="approved", date_from=per["from"], date_to=per["to"])
    pend = hr_svc.list_leave(db, status="pending")
    title = _titled(per["label"], "Staff on Leave")
    names = ", ".join(f"{r['employee']} ({r['type']})" for r in on[:6]) + (" …" if len(on) > 6 else "")
    line = (f"{title}: " + (names if on else "nobody") + "."
            + (f" {plural(len(pend), 'leave request')} waiting for approval." if pend else ""))
    rows = [{"No": r["number"], "Employee": r["employee"], "Leave": r["type"], "From": r["from_date"], "To": r["to_date"],
             "Days": r["days"], "Status": r["status"], "Reason": r["reason"]} for r in (on + pend)]
    return _out(title, str(len(on)), line, None,
                [("On leave", str(len(on))), ("Waiting for approval", str(len(pend)))],
                ["No", "Employee", "Leave", "From", "To", "Days", "Status", "Reason"], rows, None, {"tab": "hr_leave"})


def a_headcount(db, read, sc, per):
    emps = hr_svc.list_employees(db, status="current")
    joined = [e for e in hr_svc.list_employees(db, status="") if e["joined_on"] and per["from"] <= e["joined_on"] <= per["to"]]
    left = [e for e in hr_svc.list_employees(db, status="") if e["last_working_day"] and per["from"] <= e["last_working_day"] <= per["to"]
            and e["status"] != "active"]
    by = {}
    for e in emps:
        by[e["department"] or "—"] = by.get(e["department"] or "—", 0) + 1
    parts = ", ".join(f"{n} {d}" for d, n in sorted(by.items(), key=lambda x: -x[1])[:5])
    line = (f"Headcount: {plural(len(emps), 'person', 'people')}" + (f" — {parts}" if parts else "") + ". "
            f"{_titled(per['label'], 'Joined')}: {len(joined)}, left: {len(left)}.")
    t = f" {(read.get('question') or '').lower()} "
    rows = joined if _has(t, ("join",)) else left if _has(t, ("left", "resign", "exit", "attrition")) else emps
    return _out("Headcount", str(len(emps)), line, None,
                [("Working", str(len(emps))), ("Joined", str(len(joined))), ("Left", str(len(left))),
                 ("Monthly gross", inr(sum(e["gross"] for e in emps)))],
                ["Code", "Name", "Designation", "Department", "Location", "Joined", "Status"],
                [{"Code": e["code"], "Name": e["name"], "Designation": e["designation"], "Department": e["department"],
                  "Location": e["location"], "Joined": e["joined_on"], "Status": e["status"]} for e in rows],
                None, {"tab": "hr_employees"})


def a_employee(db, e):
    """One employee's record, in a line."""
    full = hr_svc.get_employee(db, e.id)
    bal = ", ".join(f"{b['code']} {b['balance']:g}" for b in full["leave"] if b["balance"])
    line = (f"{full['name']} ({full['code']}): {full['designation'] or 'no designation'}"
            + (f", {full['department']}" if full["department"] else "") + (f" at {full['location']}" if full["location"] else "")
            + f"; joined {_local_day(full['joined_on'])}, {inr(full['gross'])} a month"
            + (f"; reports to {full['manager']}" if full["manager"] else "") + ".")
    facts = [("Status", full["status"]), ("Mobile", full["phone"]), ("Service", full["service"]),
             ("Gross / month", inr(full["gross"])), ("Leave in hand", bal or None), ("Shift", full["shift"]),
             ("Store login", full["store_username"]),
             ("Advances due", inr(sum(a["balance"] for a in full["advances"] if a["status"] == "open")) if any(
                 a["status"] == "open" for a in full["advances"]) else None)]
    rows = [{"Date": h["effective_on"], "Change": h["kind"], "Detail": h["detail"]} for h in full["history"][:10]]
    return _out(full["name"], inr(full["gross"]), line, None, facts, ["Date", "Change", "Detail"], rows,
                "Their role history, newest first.", {"tab": "hr_employees"})


def _local_day(iso_day):
    try:
        return business_day.parse_day(iso_day).strftime("%d-%m-%Y")
    except Exception:                                  # noqa: BLE001
        return iso_day or "—"


# ---------------------------------------------------------------------------
#  The whole answer
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
#  The CRM — customers, orders, vouchers, coupons, loyalty, feedback, leads
# ---------------------------------------------------------------------------
_ORDER_NO = re.compile(r"^ORD-\d{3,}$", re.I)
_GV_CODE = re.compile(r"^GV[0-9A-F]{8}$", re.I)
_GV_SALE = re.compile(r"^GVS-\d{3,}$", re.I)


def _crm_code(question):
    """("customer", phone/code) or ("crm", order/voucher number) named in a question,
    or None. Checked BEFORE the trace: a 10-digit number is a mobile, not a tag."""
    q = (question or "").strip()
    for tok in q.split():
        tok = tok.strip("?.!,\"'“”()")
        if _ORDER_NO.match(tok) or _GV_CODE.match(tok) or _GV_SALE.match(tok):
            return ("crm", tok.upper())
        if re.match(r"^CUST\d{1,9}$", tok, re.I):
            return ("customer", tok.upper())
    ph = crm.phone_in(q)
    if ph and len(ph.lstrip("+")) in (10, 12, 13):
        return ("customer", ph)
    return None


def _crm_loc(sc):
    return (sc.get("at") or {}).get("location_ids") if sc else None


def _crm_off():
    return _out("CRM", "—", "The stores' database cannot be read from here, so there are no customer "
                "figures to answer with.", ok=False)


def _profile(c):
    pts = c["points"] or 0
    line = (f"{c['name']} ({c['code']}): {inr(c['spent'] or c['billed'])} spent on "
            f"{plural(c['bills'], 'bill')}, {fmt_qty(pts)} loyalty points"
            + (f", {plural(c['open_orders'], 'open order')}" if c["open_orders"] else "") + ".")
    facts = [("Mobile", c["phone"] or "—"), ("Customer code", c["code"]), ("Loyalty card", c["card"] or c["code"]),
             ("Category", c["category"]), ("City", c["city"]), ("Points", fmt_qty(pts)),
             ("Lifetime spend", inr(c["spent"] or c["billed"])),
             ("Last bill", _local_time(c["last_bill"]) if c["last_bill"] else "never"),
             ("Open orders", str(c["open_orders"]) if c["open_orders"] else None),
             ("Status", "awaiting approval" if c["awaiting"] else None)]
    rows = [{"Bill": r["bill"], "When": _local_time(r["when"]), "Total": r["total"]} for r in c["recent"]]
    return _out(c["name"], fmt_qty(pts) + " pts", line, None, facts, ["Bill", "When", "Total"], rows,
                "The ten most recent bills.", {"tab": "pos:customers"})


def a_customer(db, read, sc, per):
    if not crm.available():
        return _crm_off()
    asked = read.get("code") or read.get("person") or read.get("item") or ""
    c = crm.find_customer(asked) or crm.find_customer(read.get("question", ""))
    if c is None:
        hits = crm.find_customers_named(asked) if asked else []
        if len(hits) == 1:
            c = crm.find_customer(hits[0]["phone"] or "") if hits[0]["phone"] else None
        if c is None and hits:
            return _out("Customers", str(len(hits)), f"{plural(len(hits), 'customer')} match “{asked}” — "
                        "which one?", None, None, ["Customer", "Mobile", "Points", "Lifetime spend"],
                        [{"Customer": h["name"], "Mobile": h["phone"] or "—", "Points": h["points"],
                          "Lifetime spend": h["spent"]} for h in hits], None, {"tab": "pos:customers"})
    if c is None:
        return _out("Customer", "—", f"No customer found for “{asked or read.get('question', '')}”.",
                    ok=False, open_={"tab": "pos:customers"})
    return _profile(c)


def a_customers(db, read, sc, per):
    if not crm.available():
        return _crm_off()
    r = crm.customers(per["start"], per["end"], _crm_loc(sc))
    title = _titled(per["label"], "Customers")
    if read.get("order") or _has(f" {(read.get('question') or '').lower()} ", _RANK):
        line = (f"{_titled(per['label'], 'Top customers')}: "
                + (f"{r['top'][0]['Customer']} leads with {inr(r['top'][0]['Spent'])} on "
                   f"{plural(r['top'][0]['Bills'], 'bill')}." if r["top"] else "nobody bought in this period."))
        return _out(_titled(per["label"], "Top customers"), inr(r["top"][0]["Spent"]) if r["top"] else "—",
                    line, None, None, ["Customer", "Mobile", "Bills", "Spent"], r["top"],
                    "By what they spent in the period; cancelled bills are left out.",
                    {"tab": "pos:crm_analyser"})
    line = (f"{title}: {plural(r['new'], 'new customer')}, {plural(r['buyers'], 'customer')} bought "
            f"({r['repeat']} more than once). {r['active']:,} active customers in all"
            + (f"; {plural(r['pending'], 'sign-up')} waiting for approval." if r["pending"] else "."))
    return _out(title, f"{r['new']:,} new", line, None,
                [("Active customers", f"{r['active']:,}"), ("New", str(r["new"])),
                 ("Bought", str(r["buyers"])), ("Came back", str(r["repeat"])),
                 ("Awaiting approval", str(r["pending"]) if r["pending"] else None)],
                ["Customer", "Mobile", "City", "Category", "Joined via", "Joined"],
                [{**x, "Joined": _local_time(x["Joined"])} for x in r["rows"]],
                "The customers who joined in the period. The customer book is company-wide.",
                {"tab": "pos:crm_analyser"})


def a_birthdays(db, read, sc, per):
    if not crm.available():
        return _crm_off()
    t = f" {(read.get('question') or '').lower()} "
    kind = "anniversary" if "anniversar" in t else "birthday"
    if read.get("date_from") or read.get("date_to"):
        d0 = business_day.parse_day(per["from"])
        d1 = business_day.parse_day(per["to"])
        when = per["label"]
    else:
        d0 = business_day.today()
        d1 = d0 + timedelta(days=7)
        when = "the next 7 days"
    rows = crm.occasions(kind, d0, d1)
    noun = "Birthdays" if kind == "birthday" else "Wedding anniversaries"
    line = f"{noun} {_phrase(when) if when != 'the next 7 days' else 'in ' + when}: {plural(len(rows), 'customer')}."
    return _out(noun, str(len(rows)), line, None, None, ["Customer", "Mobile", "On", "Lifetime spend"],
                rows, "Queue wishes for them under Customers → Messages.", {"tab": "pos:send_sms"})


def a_customer_orders(db, read, sc, per):
    if not crm.available():
        return _crm_off()
    d0, d1 = business_day.parse_day(per["from"]), business_day.parse_day(per["to"])
    r = crm.orders(d0, d1, _crm_loc(sc))
    t = f" {(read.get('question') or '').lower()} "
    pending = _has(t, ("pending", "open", "due", "overdue", "deliver", "ready", "outstanding", "not delivered"))
    title = "Open customer orders" if pending else _titled(per["label"], "Customer Orders")
    line = (f"{title}: " + (f"{plural(len(r['open']), 'order')} open worth {inr(r['open_value'])} "
                            f"({inr(r['open_balance'])} still to collect)"
                            if pending else f"{plural(len(r['taken']), 'order')} taken worth {inr(r['taken_value'])}")
            + f"; {plural(len(r['due']), 'order')} due or overdue, {len(r['ready'])} ready for pickup.")
    rows = r["due"] if _has(t, ("due", "overdue")) else (r["open"] if pending else r["taken"])
    return _out(title, str(len(r["open"] if pending else r["taken"])), line, None,
                [("Taken", f"{len(r['taken'])} · {inr(r['taken_value'])}"), ("Open", str(len(r["open"]))),
                 ("Due / overdue", str(len(r["due"]))), ("Ready", str(len(r["ready"]))),
                 ("To collect", inr(r["open_balance"]))],
                ["Order", "Ordered", "Deliver by", "Customer", "Mobile", "Status", "Value", "Paid", "Balance"],
                rows, None, {"tab": "pos:crm_orders"})


def a_gift_vouchers(db, read, sc, per):
    if not crm.available():
        return _crm_off()
    d0, d1 = business_day.parse_day(per["from"]), business_day.parse_day(per["to"])
    r = crm.gift_vouchers(d0, d1, per["start"], per["end"], _crm_loc(sc))
    title = _titled(per["label"], "Gift Vouchers")
    line = (f"{title}: {plural(r['sold'], 'voucher')} sold worth {inr(r['face'])} "
            f"({inr(r['received'])} received); {inr(r['redeemed'])} redeemed on "
            f"{plural(r['redeemed_bills'], 'bill')}. {inr(r['outstanding'])} is still unspent on "
            f"{plural(r['live_vouchers'], 'voucher')}.")
    return _out(title, inr(r["face"]), line, None,
                [("Sold", str(r["sold"])), ("Face value", inr(r["face"])), ("Received", inr(r["received"])),
                 ("Redeemed", inr(r["redeemed"])), ("Unspent (all time)", inr(r["outstanding"]))],
                ["Sale", "Issued", "Bought by", "Mobile", "Mode", "Vouchers", "Face value", "Received", "Paid by"],
                r["rows"], "Unspent is what customers can still spend at the tills — a liability until they do.",
                {"tab": "pos:gift_vouchers"})


def a_coupons(db, read, sc, per):
    if not crm.available():
        return _crm_off()
    r = crm.coupons(per["start"], per["end"])
    title = _titled(per["label"], "Gift Coupons")
    line = (f"{title}: {plural(r['issued'], 'coupon')} issued, {r['redeemed']} redeemed, "
            f"taking {inr(r['discount'])} off bills.")
    return _out(title, inr(r["discount"]), line, None,
                [("Issued", str(r["issued"])), ("Redeemed", str(r["redeemed"])), ("Discount", inr(r["discount"]))],
                ["Campaign", "Redeemed", "Discount given"], r["rows"], None, {"tab": "pos:gift_coupons"})


def a_loyalty(db, read, sc, per):
    if not crm.available():
        return _crm_off()
    r = crm.loyalty(per["start"], per["end"])
    title = _titled(per["label"], "Loyalty Points")
    line = (f"{title}: {fmt_qty(r['earned'])} points earned, {fmt_qty(r['redeemed'])} redeemed. "
            f"{fmt_qty(r['held'])} points are held by {plural(r['holders'], 'customer')}.")
    return _out(title, fmt_qty(r["earned"]), line, None,
                [("Earned", fmt_qty(r["earned"])), ("Redeemed", fmt_qty(r["redeemed"])),
                 ("Adjusted / to coupons", fmt_qty(r["other"]) if r["other"] else None),
                 ("Held now", fmt_qty(r["held"])), ("Customers holding", str(r["holders"]))],
                ["Customer", "Mobile", "Points", "Lifetime spend"], r["rows"],
                "The twenty biggest balances. Loyalty is company-wide.", {"tab": "pos:loyalty"})


def a_feedback(db, read, sc, per):
    if not crm.available():
        return _crm_off()
    r = crm.feedback(per["start"], per["end"])
    title = _titled(per["label"], "Customer Feedback")
    if not r["count"]:
        return _out(title, "—", f"{title}: no responses yet.", None, None, [], [], None,
                    {"tab": "pos:feedback_dashboard"})
    line = (f"{title}: {plural(r['count'], 'response')}, averaging {r['average']:.1f} stars — "
            f"{r['happy']} happy, {r['unhappy']} unhappy to call back.")
    t = f" {(read.get('question') or '').lower()} "
    by_q = _has(t, ("question", "staff", "billing", "trial", "range", "each"))
    rows, cols = ((r["questions"], ["Question", "Answers", "Score"]) if by_q or not r["low"]
                  else ([{**x, "When": _local_time(x["When"])} for x in r["low"]],
                        ["When", "Customer", "Mobile", "Rating", "Comment"]))
    return _out(title, f"{r['average']:.1f} ★", line, None,
                [("Responses", str(r["count"])), ("Average", f"{r['average']:.2f} ★"),
                 ("Happy (4–5 ★)", str(r["happy"])), ("Unhappy", str(r["unhappy"]))],
                cols, rows, None, {"tab": "pos:feedback_dashboard"})


def a_leads(db, read, sc, per):
    if not crm.available():
        return _crm_off()
    r = crm.leads(per["start"], per["end"], _crm_loc(sc))
    line = (f"{plural(r['open'], 'open lead')} worth {inr(r['pipeline'])}: {r['due']} to follow up today, "
            f"{r['overdue']} overdue. {_titled(per['label'], 'New')}: {r['new']}; won {r['won']}, lost {r['lost']}.")
    return _out("Leads", str(r["open"]), line, None,
                [("Open", str(r["open"])), ("Due today", str(r["due"])), ("Overdue", str(r["overdue"])),
                 ("Pipeline", inr(r["pipeline"])), ("New", str(r["new"])), ("Won", str(r["won"])),
                 ("Lost", str(r["lost"]))],
                ["Lead", "Mobile", "Source", "Interested in", "Expected", "Status", "Follow up", "Assigned to"],
                r["rows"], None, {"tab": "pos:leads"})


def a_walkins(db, read, sc, per):
    if not crm.available():
        return _crm_off()
    d0, d1 = business_day.parse_day(per["from"]), business_day.parse_day(per["to"])
    r = crm.walkins(d0, d1, _crm_loc(sc))
    title = _titled(per["label"], "Walk-ins")
    rate = f" ({r['billed'] / r['total'] * 100:.0f}%)" if r["total"] else ""
    line = (f"{title}: {plural(r['total'], 'customer')} walked in, {r['billed']} bought{rate} for "
            f"{inr(r['bought'])}, {r['left']} left without buying"
            + (f", {r['open']} still on the floor." if r["open"] else "."))
    return _out(title, str(r["total"]), line, None,
                [("Walked in", str(r["total"])), ("Bought", str(r["billed"])), ("Left", str(r["left"])),
                 ("On the floor", str(r["open"]) if r["open"] else None), ("Sales", inr(r["bought"]))],
                ["In at", "Customer", "Mobile", "Looking for", "Floor", "Salesperson", "Status", "Bill", "Bought"],
                r["rows"], None, {"tab": "pos:customer_divert"})


def a_crm_code(code):
    """An order number or a gift voucher / voucher sale code."""
    if _ORDER_NO.match(code):
        o = crm.find_order(code)
        if o:
            line = (f"Order {o['Order']} for {o['Customer']}: {inr(o['Value'])}, {inr(o['Paid'])} paid, "
                    f"{o['Status']}" + (f", deliver by {o['Deliver by']}" if o["Deliver by"] != "—" else "") + ".")
            return _out(f"Order {o['Order']}", o["Status"], line, None, [(k, str(v)) for k, v in o.items()],
                        [], [], None, {"tab": "pos:crm_orders"})
    elif _GV_CODE.match(code):
        v = crm.find_voucher(code)
        if v:
            state = "cancelled" if not v["active"] else ("spent" if v["left"] <= 0 else f"{inr(v['left'])} left")
            line = f"Gift voucher {v['code']}: worth {inr(v['value'])}, {inr(v['spent'])} spent — {state}."
            return _out(f"Gift voucher {v['code']}", inr(v["left"]), line, None,
                        [("Value", inr(v["value"])), ("Spent", inr(v["spent"])), ("Left", inr(v["left"])),
                         ("Valid till", v["valid_to"] or "no expiry"), ("Bought by", v["buyer"]),
                         ("For", v["given_to"]), ("Sale", v["sale"])], [], [], None,
                        {"tab": "pos:gift_vouchers"})
    elif _GV_SALE.match(code):
        return _out(f"Gift voucher sale {code}", "", f"{code} is a gift voucher sale — open Gift Voucher → "
                    "Transaction History and search it to see each voucher.", open_={"tab": "pos:gift_vouchers"})
    return None


_DEFAULT_PERIOD = {"sales": "today", "sales_rank": "today", "discounts": "today", "compare": "month",
                   "profit": "today", "purchases": "today", "payments": "today",
                   "returns": "today", "transfers": "today", "activity": "today",
                   "invoices": "today", "lr": "today",
                   "customers": "month", "customer_orders": "today", "gift_vouchers": "month",
                   "coupons": "month", "loyalty": "month", "feedback": "month", "leads": "month",
                   "walkins": "today", "birthdays": "today", "bills": "today",
                   "attendance": "today", "staff_leave": "today", "headcount": "month"}


def ask(db, question, role="admin", allowed=None, hr=False):
    """Read the question, answer it in one line, keep the rows behind it.
    Always returns a dict; never raises for a question it cannot answer."""
    question = (question or "").strip()
    if not question:
        return {"ok": False, "question": question, "line": "Ask something — or press the microphone.",
                "interpretation": {"engine": "none", "reading": "nothing was asked"}}

    read = interpret(db, question)
    intent = read.get("intent") or NONE
    interp = {"intent": intent, "reading": read.get("reading") or "",
              "engine": read.get("engine"), "confidence": read.get("confidence"),
              "degraded": read.get("degraded"),
              "applied": {k: read.get(k) for k in ("date_from", "date_to", "warehouse", "store",
                                                   "floor", "counter", "person", "person_role",
                                                   "item", "code", "group_by", "order", "days")
                          if read.get(k)},
              "notes": []}

    # A CRM document number (an order, a gift voucher) is the CRM's to answer,
    # not the warehouse trace's.
    if intent == "trace":
        crm_hit = _crm_code(read.get("code") or question)
        if crm_hit and crm_hit[0] == "customer":
            intent = interp["intent"] = "customer"
            read["code"] = crm_hit[1]
        elif crm_hit:
            out = a_crm_code(crm_hit[1])
            if out:
                return {**out, "question": question, "intent": "trace", "interpretation": interp}

    if intent == "trace":
        out = trace_svc.find(db, read.get("code") or question, allowed=allowed)
        if not out.get("ok", True) and not out.get("choices") and pos_insights.available():
            # not a warehouse code — perhaps a store product's own SKU or barcode
            code = (read.get("code") or question).strip()
            hit = pos_insights.products_named(code) if " " not in code else []
            if hit and any(code.lower() in (_norm(p["sku"]), _norm(p["barcode"])) for p in hit):
                try:
                    found = a_product(db, code, _scope(db, {}, allowed), _period(read, "month"), hit, [])
                except Refused:
                    found = None
                if found:
                    interp["intent"] = "stock"
                    return {**found, "question": question, "intent": "stock", "interpretation": interp}
        out.setdefault("title", "Track")
        out.setdefault("headline", "")
        out.setdefault("facts", [])
        out.setdefault("columns", [])
        out.setdefault("rows", [])
        return {**out, "question": question, "intent": "trace", "interpretation": interp}

    if intent == NONE or intent not in INTENTS:
        # Last try before giving up: a question that is only a name is asking
        # about whoever or whatever has it — anywhere in the software.
        found = _lookup(db, question, read, role, allowed, hr)
        if found:
            interp["intent"] = found[0]
            interp["reading"] = interp["reading"] if interp["engine"] == "model" else "looked the name up everywhere"
            return {**found[1], "question": question, "intent": found[0], "interpretation": interp}
    if intent == NONE or intent not in INTENTS:
        return {"ok": False, "question": question, "intent": NONE, "title": "Not sure",
                "line": "I couldn't match that to something I can answer yet — try one of the examples, "
                        "or type a GRN, bill or QR code to track it.",
                "interpretation": interp, "suggestions": [e["q"] for e in examples()[:6]]}

    if intent in HR_INTENTS and not hr:
        return {"ok": False, "question": question, "intent": intent, "title": "Not allotted",
                "line": "Pay, attendance and leave are on HR, which this account is not given (Users & Access).",
                "interpretation": interp}
    try:
        sc = _scope(db, read, allowed)
    except Refused as r:
        return {"ok": False, "question": question, "intent": intent, "title": "Not allotted",
                "line": str(r), "interpretation": interp}
    interp["notes"] = sc["notes"]
    if intent in _PERSONAL and read.get("person") and _who(db, read, sc) is False:
        return {**_nobody(read["person"]), "question": question, "intent": intent,
                "interpretation": interp}

    per = _period(read, _DEFAULT_PERIOD.get(intent, "today"))
    builders = {
        "sales": a_sales, "sales_rank": a_sales_rank, "discounts": a_discounts,
        "profit": a_profit, "purchases": a_purchases, "payments": a_payments, "compare": a_compare,
        "invoices": a_invoices, "lr": a_lr, "bills": a_bills,
        "pending_payments": a_pending_payments, "pending_grns": a_pending_grns,
        "returns": a_returns, "stock": a_stock, "stock_rank": a_stock_rank,
        "low_stock": a_low_stock, "dead_stock": a_dead_stock, "transfers": a_transfers,
        "customer": a_customer, "customers": a_customers, "birthdays": a_birthdays,
        "customer_orders": a_customer_orders, "gift_vouchers": a_gift_vouchers, "coupons": a_coupons,
        "loyalty": a_loyalty, "feedback": a_feedback, "leads": a_leads, "walkins": a_walkins,
        "payroll": a_payroll, "attendance": a_attendance, "staff_leave": a_staff_leave, "headcount": a_headcount,
    }
    read["question"] = question
    try:
        if intent == "counts":
            out = a_counts(db, read, sc, per, role)
        elif intent == "activity":
            out = a_activity(db, read, sc, per, role, allowed)
        else:
            out = builders[intent](db, read, sc, per)
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "question": question, "intent": intent, "title": "Could not answer",
                "line": f"Something went wrong working that out ({type(e).__name__}). "
                        "The figures on the dashboards are unaffected.",
                "interpretation": interp}

    if intent == "customer" and not out.get("ok", True):
        # Not a customer — perhaps somebody or something else by that name.
        found = _lookup(db, question, read, role, allowed, hr)
        if found:
            interp["intent"] = found[0]
            return {**found[1], "question": question, "intent": found[0], "interpretation": interp}

    timeless = intent in ("pending_payments", "pending_grns", "stock", "stock_rank",
                          "low_stock", "dead_stock", "counts", "customer", "birthdays")
    return {**out, "question": question, "intent": intent,
            "period": None if timeless else {k: per[k] for k in ("from", "to", "label")},
            "interpretation": interp}


def examples():
    """Questions that work — the ones from the brief, and the shape of each answer."""
    return [
        {"q": "Today's total sales", "note": "one line, every store"},
        {"q": "Today's total invoices", "note": "supplier invoices entered"},
        {"q": "Today's total LR entries", "note": "consignments booked in"},
        {"q": "Which floor has the highest sales?", "note": "ranked"},
        {"q": "How much stock is available in Ground Floor?", "note": "store floor"},
        {"q": "Show sales of ladies shirts this month", "note": "by category words"},
        {"q": "Which products are low in stock?", "note": "reorder level"},
        {"q": "Show products with no sales for 90 days", "note": "dead stock"},
        {"q": "Who gave the highest discount today?", "note": "by cashier"},
        {"q": "Show pending supplier payments", "note": "outstanding"},
        {"q": "How many products were received through GRN today?", "note": "GRN receipts"},
        {"q": "Which warehouse has the highest stock value?", "note": "ranked"},
        {"q": "What did people do today?", "note": "audit trail"},
        {"q": "Today's profit", "note": "margin at cost"},
        {"q": "How many new customers this month?", "note": "CRM · customer book"},
        {"q": "Pending customer orders", "note": "CRM · orders"},
        {"q": "Gift vouchers sold this month", "note": "CRM · vouchers"},
        {"q": "Customer feedback this week", "note": "CRM · ratings"},
        {"q": "Leads to follow up today", "note": "CRM · leads"},
        {"q": "Today's walk-ins", "note": "CRM · footfall"},
        {"q": "Birthdays this week", "note": "CRM · wishes"},
        {"q": "Loyalty points of 9876543210", "note": "CRM · one customer"},
        {"q": "Bills billed by Priya today", "note": "one cashier's bills"},
        {"q": "How much did Priya sell this month?", "note": "one person's sales"},
        {"q": "Show R Kumar's bills this month", "note": "one customer's bills"},
        {"q": "Priya", "note": "any name — staff, customer, supplier, store, product"},
        {"q": "Who is absent today?", "note": "HR · attendance"},
        {"q": "Who is on leave today?", "note": "HR · leave"},
        {"q": "Last month's payroll", "note": "HR · salaries"},
        {"q": "Headcount", "note": "HR · people"},
    ]
