"""
Natural-language report queries — "ask the warehouse a question".

Someone types "how much did we buy from AMS Garments last month" or
"கடந்த மாதம் வாங்கிய பொருட்கள்" and gets the right report, already filtered.

WHY THIS ROUTES INSTEAD OF WRITING SQL
--------------------------------------
The obvious build is text-to-SQL: hand the model the schema and run what it
writes. This does not do that, and the reason is not squeamishness about
generated SQL — it is that the answer would be worse.

`services/reports.py` already holds 33 curated reports. Each one knows what it
is counting and, where the honest figure differs from the obvious one, says so
in its `note` — that only stock traceable to a posted GRN is stock, that a
broken-down bundle received its variants rather than itself, that a shortage is
claimable until waived. A generated query knows the columns and none of that. It
would return a number that looks right, disagrees with the same figure on the
Inventory screen, and gives nobody a way to tell which is wrong.

So the model's job here is narrow and checkable: pick one of the 33 reports and
fill in its filters. That has three consequences worth having — the answer comes
back in the same {columns, rows, totals, note} shape the screen and the CSV
export already render, an arbitrary question cannot touch the database in any way
the Reports screen could not already, and the reading is small enough to show the
user in one line so they can catch a misread before trusting the figures.

WHAT THE MODEL CANNOT GET WRONG
-------------------------------
`report_key` is an `enum` in the structured-output schema, so an unknown report
is not a thing the model can return — no validation branch, no 404 path. Dates
come back as ISO and are still re-parsed through services/dates.py, because a
plausible-looking date is exactly the kind of thing to check rather than trust.

FILTERS THE REPORTS DO NOT HAVE
-------------------------------
Only `stock_movement` accepts a product or a movement kind, roughly half accept a
date range, and **nothing accepts a supplier**. So "purchases from AMS Garments"
routed to the Purchase Report would return every supplier's purchases, and
labelling that with the question would be a lie.

Two things happen instead. Routing prefers a report that can honour the filters
the question implies. Then anything left over is applied by narrowing the rows
after the report runs (`_narrow`), which is reported as a separate step and never
silently folded into "here is your answer".

WITHOUT AN API KEY
------------------
Vision is optional in this app and so is this. `_offline()` scores the question
against each report's keywords and resolves relative dates itself. It is visibly
worse — it is matching words, not reading a sentence — so it says so in the
`engine` field rather than presenting itself as the same feature.
"""
import os
import json
import re
import datetime as dt
import difflib

from .. import models
from .. import runtime
from . import reports as reports_svc
from . import dates as date_svc

#: Text classification, not extraction from an image — so this is deliberately
#: not the vision model the Settings screen configures. That one is chosen for
#: reading photographs of invoices and may be set to a model with no structured
#: output support at all, which this depends on.
NLQ_MODEL = os.environ.get("ESSA_NLQ_MODEL", "claude-opus-5")

#: Movement kinds `stock_movement` filters on — the values services/inventory.py
#: and services/outward.py actually write.
MOVEMENT_KINDS = ("inward", "outward", "return", "adjustment", "reversal")

NO_MATCH = "none"


# ---------------------------------------------------------------------------
#  What each report answers
# ---------------------------------------------------------------------------
#: key -> (what it answers, english keywords, tamil keywords)
#:
#: The first element is for the model: one line on what question this report is
#: the answer to. The keyword lists are for the offline fallback only, which has
#: no way to read a sentence and can only count word hits.
#:
#: Tamil here is a pragmatic keyword list for that fallback, not a translation
#: layer — real Tamil understanding is the model's job, and a question phrased in
#: Tamil that misses these words still routes correctly when a key is set.
HINTS = {
    # --- transport ---
    "transport_report": (
        "Consignments in the LR/transport register: transporter, LR number, pieces, freight.",
        ("transport", "lorry", "lr", "consignment", "freight", "courier", "docket"),
        ("போக்குவரத்து", "லாரி", "சரக்கு கட்டணம்", "கன்சைன்மென்ட்"),
    ),
    "transport_pending_bills": (
        "Freight still owed to transporters — unpaid or to-pay consignments.",
        ("transport pending", "freight pending", "freight due", "topay", "to pay", "transport outstanding"),
        ("நிலுவை சரக்கு கட்டணம்", "போக்குவரத்து நிலுவை"),
    ),
    # --- invoice ---
    "invoice_report": (
        "Supplier invoices as documents: number, date, supplier, totals, extraction status.",
        ("invoice", "bill", "document", "invoices"),
        ("விலைப்பட்டியல்", "பில்", "இன்வாய்ஸ்"),
    ),
    "invoice_detail_report": (
        "Every line item on every invoice — description, HSN, qty, rate, amount.",
        ("invoice detail", "invoice line", "line item", "item wise invoice", "invoice items"),
        ("விலைப்பட்டியல் விவரம்", "வரிசை விவரம்"),
    ),
    "wh_entry_report": (
        "What the warehouse entered against each invoice — the intake/entry register.",
        ("wh entry", "warehouse entry", "entry register", "intake"),
        ("கிடங்கு பதிவு", "பதிவு"),
    ),
    # --- stock ---
    "stock_report": (
        "Stock on hand right now: per product qty, average cost and stock value.",
        ("stock", "on hand", "inventory", "stock in hand", "how much stock", "closing stock", "stock value"),
        ("இருப்பு", "சரக்கு", "கையிருப்பு", "இருப்பு மதிப்பு"),
    ),
    "stock_as_on": (
        "Stock as it stood on one particular date (needs that date).",
        ("as on", "as of", "stock on date", "stock as on"),
        ("அன்றைய இருப்பு", "தேதி இருப்பு"),
    ),
    "stock_transactions": (
        "Every stock transaction in a date range — what came in and went out.",
        ("stock transaction", "transactions", "stock ledger", "in and out"),
        ("பரிவர்த்தனை", "இருப்பு பரிவர்த்தனை"),
    ),
    "stock_movement": (
        "Individual stock movements, filterable by kind (inward/outward/return/"
        "adjustment/reversal) and by one product.",
        ("movement", "stock movement", "moved", "inward", "outward", "adjustment", "reversal"),
        ("இயக்கம்", "நகர்வு", "உள்வரவு", "வெளிச்செல்லல்"),
    ),
    "stock_by_location": (
        "Stock movement broken down by location / rack / section.",
        ("location", "locationwise", "rack", "section wise stock", "godown", "by location"),
        ("இடம்", "ரேக்", "பிரிவு"),
    ),
    "warehouse_stock_analysis": (
        "Warehouse-level stock analysis — a summary view across the whole godown.",
        ("stock analysis", "warehouse analysis", "analysis"),
        ("பகுப்பாய்வு", "கிடங்கு பகுப்பாய்வு"),
    ),
    "stock_audit_report": (
        "Stock audit — records whose figures do not reconcile and need checking.",
        ("audit", "stock audit", "reconcile", "mismatch", "discrepancy"),
        ("தணிக்கை", "வேறுபாடு"),
    ),
    # --- purchase ---
    "purchase_register": (
        "The purchase register: one row per posted GRN with totals, paid, returns "
        "and outstanding. The headline 'what did we buy' / 'how much did we spend' report.",
        ("purchase", "purchases", "buy", "bought", "buying", "grn", "purchase register",
         "goods receipt", "spend", "spent", "procure", "procured"),
        ("கொள்முதல்", "வாங்கிய", "வாங்கியது", "வாங்கினோம்", "ஜிஆர்என்", "செலவு"),
    ),
    "purchase_items_report": (
        "Purchased items line by line over a date range — which goods were bought.",
        ("purchase item", "items bought", "which items", "item wise purchase", "goods bought",
         "products bought", "items purchased", "what items"),
        ("வாங்கிய பொருட்கள்", "பொருட்கள் கொள்முதல்"),
    ),
    "purchase_hsn_report": (
        "Purchases grouped by HSN code.",
        ("hsn", "hsn wise", "hsn code"),
        ("எச்எஸ்என்",),
    ),
    "purchase_tax_report": (
        "Tax on each purchase — CGST/SGST/IGST per invoice.",
        ("tax", "gst", "cgst", "sgst", "igst", "tax report"),
        ("வரி", "ஜிஎஸ்டி"),
    ),
    "purchase_tax_summary": (
        "Purchase tax totalled up by rate — the summary rather than per invoice.",
        ("tax summary", "gst summary", "tax total", "summary of tax"),
        ("வரி சுருக்கம்", "வரி மொத்தம்"),
    ),
    "purchase_barcode_wise": (
        "Purchases by supplier barcode / SKU.",
        ("barcode", "barcode wise", "sku wise", "by barcode"),
        ("பார்கோடு",),
    ),
    "section_wise_purchase": (
        "Purchases grouped by category section.",
        ("section wise purchase", "category wise purchase", "by section", "by category"),
        ("பிரிவு வாரியாக", "வகை வாரியாக"),
    ),
    "supplier_pending_bills": (
        "Unpaid supplier bills with days outstanding — who we owe and how much.",
        ("pending", "outstanding", "payable", "owe", "unpaid", "due", "pending bills", "how much do we owe"),
        ("நிலுவை", "பாக்கி", "கொடுக்க வேண்டிய", "நிலுவைத் தொகை"),
    ),
    "payments_register": (
        "Payments made to suppliers — receipts, mode, amount, which bills they settled.",
        ("payment", "paid", "payments", "receipt", "settled", "cheque", "neft"),
        ("பணம்", "செலுத்திய", "பேமெண்ட்", "காசோலை"),
    ),
    "grn_shortage_register": (
        "Goods billed but not delivered — shortages recorded at the dock, and "
        "whether each has been claimed or waived.",
        ("shortage", "short", "missing", "not received", "shortfall", "claim"),
        ("பற்றாக்குறை", "குறைவு", "வரவில்லை"),
    ),
    # --- purchase return ---
    "purchase_return_register": (
        "Purchase returns / debit notes raised against suppliers.",
        ("return", "returns", "debit note", "sent back", "purchase return"),
        ("திரும்ப", "ரிட்டர்ன்", "டெபிட் நோட்"),
    ),
    "section_wise_purchase_return": (
        "Purchase returns grouped by category section.",
        ("section wise return", "return by section", "category wise return"),
        ("பிரிவு வாரியாக ரிட்டர்ன்",),
    ),
    "purchase_return_audit": (
        "Purchase return audit — how each debit note was priced and what it moved.",
        ("return audit", "debit note audit", "audit return"),
        ("ரிட்டர்ன் தணிக்கை",),
    ),
    # --- outward ---
    "outward_report": (
        "Stock dispatched out — transfers to shops or other godowns.",
        ("outward", "dispatch", "dispatched", "sent", "transfer", "sent out"),
        ("வெளியே", "அனுப்பிய", "மாற்றம்", "டிஸ்பேட்ச்"),
    ),
    "outward_details_report": (
        "Dispatched stock line by line — which pieces went on which transfer.",
        ("outward detail", "dispatch detail", "outward items", "transfer items"),
        ("அனுப்பிய விவரம்",),
    ),
    "pending_inward_report": (
        "Dispatched transfers no destination has accepted yet — stock in transit.",
        ("pending inward", "in transit", "not received", "awaiting", "not accepted", "transit"),
        ("வழியில்", "பெறப்படாத", "நிலுவை உள்வரவு"),
    ),
    "pending_outward_report": (
        "Transfers packed but not yet dispatched.",
        ("pending outward", "not dispatched", "packed", "draft outward"),
        ("அனுப்பப்படாத", "நிலுவை வெளிச்செல்லல்"),
    ),
    # --- masters ---
    "product_master": (
        "The product master — every product record and its attributes.",
        ("product master", "all products", "product list", "products", "item master"),
        ("பொருள் பட்டியல்", "பொருட்கள்"),
    ),
    "supplier_master": (
        "The supplier master — every supplier, GSTIN, state, contact.",
        ("supplier master", "all suppliers", "supplier list", "suppliers", "vendor"),
        ("விற்பனையாளர் பட்டியல்", "சப்ளையர்"),
    ),
    "agent_master": (
        "The agent master — agents and their commission.",
        ("agent", "agents", "agent master", "commission", "broker"),
        ("முகவர்", "கமிஷன்"),
    ),
    "tax_master": (
        "The tax / HSN master — HSN codes and their rates.",
        ("tax master", "hsn master", "rate master", "tax rates"),
        ("வரி பட்டியல்", "எச்எஸ்என் பட்டியல்"),
    ),
}


# ---------------------------------------------------------------------------
#  Relative dates
# ---------------------------------------------------------------------------
#: Phrases the offline path resolves itself, and the model is told today's date
#: so it can resolve the same things to ISO. Tamil forms sit alongside English.
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})

_TODAY_WORDS = ("today", "இன்று")
_YESTERDAY_WORDS = ("yesterday", "நேற்று")
_THIS_MONTH_WORDS = ("this month", "current month", "இந்த மாதம்")
_LAST_MONTH_WORDS = ("last month", "previous month", "past month", "கடந்த மாதம்", "சென்ற மாதம்")
_THIS_WEEK_WORDS = ("this week", "இந்த வாரம்")
_LAST_WEEK_WORDS = ("last week", "கடந்த வாரம்", "சென்ற வாரம்")
_THIS_YEAR_WORDS = ("this year", "இந்த ஆண்டு", "இந்த வருடம்")
_LAST_YEAR_WORDS = ("last year", "கடந்த ஆண்டு", "சென்ற ஆண்டு")


def _month_range(year, month):
    first = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), (month % 12) + 1, 1)
    return first.isoformat(), (nxt - dt.timedelta(days=1)).isoformat()


def relative_range(text, today=None):
    """(date_from, date_to) for a relative phrase in `text`, else (None, None).

    Deliberately conservative: an unrecognised phrase returns no range rather
    than a guessed one, because a report silently covering the wrong month is
    worse than one covering everything and saying so."""
    t = (text or "").lower()
    today = today or dt.date.today()

    def has(words):
        return any(w in t for w in words)

    # An explicit range — "from 01-11-2026 to 04-11-2026", "between 1 Nov and 4 Nov".
    # Read before the single-day phrases, or only its first date would be kept.
    one = r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+[a-z]{3,9}(?:\s+\d{4})?)"
    rm = re.search(rf"\b{one}\s*(?:to|till|until|and|–|—)\s*{one}", t)
    if rm:
        a, b = relative_range(rm.group(1), today), relative_range(rm.group(2), today)
        if a[0] and b[0] and a[0] == a[1] and b[0] == b[1]:
            return min(a[0], b[0]), max(a[0], b[0])

    if has(_TODAY_WORDS):
        return today.isoformat(), today.isoformat()
    if has(_YESTERDAY_WORDS):
        y = (today - dt.timedelta(days=1)).isoformat()
        return y, y
    if has(_LAST_MONTH_WORDS):
        return _month_range(today.year - (today.month == 1),
                            12 if today.month == 1 else today.month - 1)
    if has(_THIS_MONTH_WORDS):
        return _month_range(today.year, today.month)
    if has(_LAST_WEEK_WORDS):
        start = today - dt.timedelta(days=today.weekday() + 7)
        return start.isoformat(), (start + dt.timedelta(days=6)).isoformat()
    if has(_THIS_WEEK_WORDS):
        start = today - dt.timedelta(days=today.weekday())
        return start.isoformat(), today.isoformat()
    if has(_LAST_YEAR_WORDS):
        return f"{today.year - 1}-01-01", f"{today.year - 1}-12-31"
    if has(_THIS_YEAR_WORDS):
        return f"{today.year}-01-01", today.isoformat()

    m = re.search(r"(?:last|past)\s+(\d{1,3})\s*(day|days|week|weeks|month|months)", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        days = n * (1 if unit.startswith("day") else 7 if unit.startswith("week") else 30)
        return (today - dt.timedelta(days=days)).isoformat(), today.isoformat()

    # A specific day — "5 August", "5 Aug 2025", "August 5". Checked before the
    # bare month name below, or "as on 5 August" would silently widen to the whole
    # of August and answer a question nobody asked.
    for name, num in _MONTHS.items():
        dm = (re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{name}\b\s*(\d{{4}})?", t)
              or re.search(rf"\b{name}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:\s*,?\s*(\d{{4}}))?", t))
        if dm:
            day, year = int(dm.group(1)), int(dm.group(2) or today.year)
            try:
                d = dt.date(year, num, day).isoformat()
                return d, d
            except ValueError:                       # "31 February" — not a day
                pass

    # A whole month — "in August", "August 2026"
    for name, num in _MONTHS.items():
        if re.search(rf"\b{name}\b", t):
            ym = re.search(rf"\b{name}\b\s*(\d{{4}})", t)
            year = int(ym.group(1)) if ym else today.year
            return _month_range(year, num)

    # A written-out date the rest of the system already knows how to read
    dmy = re.search(r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}-\d{2}-\d{2})\b", t)
    if dmy:
        iso = date_svc.to_iso(dmy.group(1))
        if iso:
            return iso, iso

    ym = re.search(r"\b(20\d{2})\b", t)
    if ym:
        return f"{ym.group(1)}-01-01", f"{ym.group(1)}-12-31"
    return None, None


# ---------------------------------------------------------------------------
#  The model call
# ---------------------------------------------------------------------------
def _report_lines():
    """The catalogue as the model sees it. Sorted, so the prompt is byte-stable
    across processes and the cached prefix actually gets reused."""
    out = []
    for key in sorted(reports_svc.REPORTS):
        name, group, fn = reports_svc.REPORTS[key]
        what = HINTS.get(key, ("",))[0]
        params = reports_svc._params(fn)
        out.append(f"- {key} | {name} ({group}) | filters: "
                   f"{', '.join(params) if params else 'none'} | {what}")
    return "\n".join(out)


SYSTEM = """You route a warehouse manager's question to ONE report from a fixed \
catalogue and fill in that report's filters. You are the query layer of an Indian \
garment distributor's warehouse system.

The question may be in English or Tamil, or mix the two. Read either.

REPORTS (key | name (group) | filters it accepts | what it answers):
{catalogue}

RULES
1. Pick the single report that best answers the question. Use its exact key.
2. Prefer a report that ACCEPTS the filters the question implies. If the question
   restricts a date range, prefer a report whose filters include date_from and
   date_to over one that takes none — a report that cannot filter by date will
   return every period.
3. If no report answers the question, return "{no_match}" and say what was asked
   for in `reading`. Do not force a loose match. A wrong report is worse than
   an honest miss, because the person will read its numbers as their answer.
4. Dates: resolve every relative phrase against TODAY and return ISO YYYY-MM-DD.
   "last month", "கடந்த மாதம்" and "August" all become a concrete from/to.
   `as_on` is a single date and only for stock_as_on.
5. `kind` is only for stock_movement and must be one of: {kinds}.
6. `supplier_name` is the supplier as the person named them — do not correct
   spelling or expand it; it gets matched against the supplier master separately.
   `product_query` likewise for a product, SKU or barcode.
7. `reading` is one short plain-English line stating how you read the question,
   shown to the person so they can catch a misread. Name the report, the period
   and any supplier. Never claim a filter you did not set.
8. Leave any field that does not apply as an empty string.

TODAY is {today}."""


def _schema():
    return {
        "type": "object",
        "properties": {
            "report_key": {
                "type": "string",
                "enum": sorted(reports_svc.REPORTS) + [NO_MATCH],
                "description": "The report that answers the question, or 'none'.",
            },
            "reading": {"type": "string", "description": "One line: how you read the question."},
            "date_from": {"type": "string", "description": "ISO YYYY-MM-DD or empty."},
            "date_to": {"type": "string", "description": "ISO YYYY-MM-DD or empty."},
            "as_on": {"type": "string", "description": "ISO YYYY-MM-DD or empty; stock_as_on only."},
            "kind": {"type": "string", "description": "Movement kind or empty; stock_movement only."},
            "supplier_name": {"type": "string", "description": "Supplier as named, or empty."},
            "product_query": {"type": "string", "description": "Product, SKU or barcode, or empty."},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        # Structured outputs require every property listed; "" carries "absent".
        "required": ["report_key", "reading", "date_from", "date_to", "as_on",
                     "kind", "supplier_name", "product_query", "confidence"],
        "additionalProperties": False,
    }


def available():
    """Whether the model path can run at all."""
    if not runtime.get("anthropic_api_key"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _ask_model(question):
    """Route with the model. Raises on API failure so the caller can fall back."""
    import anthropic
    client = anthropic.Anthropic(api_key=runtime.get("anthropic_api_key"))
    system = SYSTEM.format(catalogue=_report_lines(), no_match=NO_MATCH,
                           kinds=", ".join(MOVEMENT_KINDS),
                           today=dt.date.today().isoformat())
    msg = client.messages.create(
        model=NLQ_MODEL,
        # Thinking is on by default on this model and `max_tokens` caps thinking
        # plus the answer, so this is not sized against the small JSON alone.
        max_tokens=4096,
        # The catalogue is identical on every question and is the bulk of the
        # prompt, so it is cached; the question — the only part that changes —
        # sits after the breakpoint in the user turn.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": question}],
        # Via extra_body rather than as a typed argument: `output_config` is newer
        # than the pinned SDK (0.40.0 does not know the parameter and would reject
        # the keyword), while the API itself honours it — it is versioned by the
        # anthropic-version header, not by the client. extra_body merges into the
        # request body on old and new SDKs alike, so this keeps working after an
        # SDK upgrade instead of becoming the wrong way to pass it.
        #
        # effort low because routing one sentence into a fixed list does not
        # reward deliberation, and someone is waiting on a table.
        extra_body={"output_config": {
            "effort": "low",
            "format": {"type": "json_schema", "schema": _schema()},
        }},
    )
    if msg.stop_reason == "refusal":
        raise RuntimeError("the model declined to answer this question")
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    data = json.loads(text)
    data["engine"] = "model"
    return data


# ---------------------------------------------------------------------------
#  The offline fallback
# ---------------------------------------------------------------------------
def _offline(question):
    """Keyword routing, for when no API key is set.

    Scores each report's keywords against the question, longest phrase first so
    that "purchase item" beats "purchase". This is word matching, not reading —
    it says so in `engine` and the screen tells the person as much."""
    q = (question or "").lower()
    lo, hi = relative_range(q)

    best, best_score = NO_MATCH, 0.0
    for key, (_what, en, ta) in HINTS.items():
        score = 0.0
        for word in en:
            if word in q:
                score += 1.0 + 0.35 * word.count(" ")   # a phrase hit beats a word hit
        for word in ta:
            if word in q:
                score += 1.5                            # Tamil terms are less ambiguous
        # The same preference rule 2 gives the model: when the question restricts
        # a period, a report that can filter by date beats an equally-matching one
        # that would silently return every period. Small enough to only break ties.
        if score > 0 and (lo or hi):
            params = reports_svc._params(reports_svc.REPORTS[key][2])
            if "date_from" in params or "date_to" in params:
                score += 0.5
        if score > best_score:
            best, best_score = key, score

    reading = (f"matched on keywords to {reports_svc.REPORTS[best][0]}"
               if best != NO_MATCH else "no report matched the words in this question")
    # A single as-on date is the same restriction as a one-day range, so only one
    # of the two is sent. Sending both means the range comes back reported as a
    # filter that could not be applied, which reads as a problem rather than as
    # the duplicate it is.
    as_on = (lo or "") if best == "stock_as_on" else ""
    if as_on:
        lo = hi = None
    return {
        "report_key": best, "reading": reading,
        "date_from": lo or "", "date_to": hi or "", "as_on": as_on,
        "kind": next((k for k in MOVEMENT_KINDS if k in q), "") if best == "stock_movement" else "",
        # No supplier or product guessing offline: picking the wrong supplier out
        # of a sentence by substring is worse than not filtering and saying so.
        "supplier_name": "", "product_query": "",
        "confidence": "high" if best_score >= 2 else "medium" if best_score >= 1 else "low",
        "engine": "keywords",
    }


def interpret(question):
    """Read the question into a report key plus filters. Never raises."""
    if available():
        try:
            return _ask_model(question)
        except Exception as e:                     # noqa: BLE001 — any failure falls back
            out = _offline(question)
            out["engine"] = "keywords"
            out["degraded"] = f"the model could not be reached ({e}); matched on keywords instead"
            return out
    return _offline(question)


# ---------------------------------------------------------------------------
#  Resolving names to ids
# ---------------------------------------------------------------------------
def _match_supplier(db, name):
    """(supplier, matched_name) for the closest supplier, or (None, None)."""
    if not name:
        return None, None
    wanted = name.strip().lower()
    rows = db.query(models.Supplier).all()
    for s in rows:                                  # exact, then contained
        if (s.name or "").lower() == wanted:
            return s, s.name
    for s in rows:
        n = (s.name or "").lower()
        if wanted and (wanted in n or n in wanted):
            return s, s.name
    names = {(s.name or "").lower(): s for s in rows}
    close = difflib.get_close_matches(wanted, list(names), n=1, cutoff=0.72)
    if close:
        s = names[close[0]]
        return s, s.name
    return None, None


def _match_product(db, text):
    if not text:
        return None, None
    wanted = text.strip().lower()
    for p in db.query(models.Product).all():
        for field in (p.sku, p.barcode, p.description):
            if field and (field.lower() == wanted or wanted in field.lower()):
                return p, p.description or p.sku
    return None, None


# ---------------------------------------------------------------------------
#  Narrowing what the reports cannot filter themselves
# ---------------------------------------------------------------------------
#: Columns a narrowing pass will match on, in preference order.
_SUPPLIER_COLS = ("supplier", "supplier_name", "vendor")
_DATE_COLS = ("date", "invoice_date", "recv_date", "lr_date", "posted_at")


def _numeric_totals(columns, rows):
    """Plain sums of the numeric columns shown.

    Not an attempt to reproduce the report's own totals — those are report
    specific (counts, averages, derived values) and inventing them for a subset
    of rows would produce a figure nobody can check. These are sums of the
    columns on screen, so anyone can add the column up and get the same number."""
    out = {}
    for c in columns:
        vals = [r.get(c) for r in rows]
        if any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            out[c] = round(sum(v for v in vals if isinstance(v, (int, float))
                               and not isinstance(v, bool)), 2)
    out["rows"] = len(rows)
    return out


def _narrow(rep, supplier=None, date_from=None, date_to=None):
    """Narrow rows the report could not filter itself.

    Returns (rep, [descriptions of what was narrowed]). Dates compare as strings
    because every date in this system is stored ISO, where lexical order IS
    chronological — the same property services/reports.py relies on."""
    applied = []
    rows = rep["rows"]
    cols = rep["columns"]

    if supplier:
        col = next((c for c in _SUPPLIER_COLS if c in cols), None)
        if col:
            want = supplier.lower()
            rows = [r for r in rows if want in str(r.get(col) or "").lower()]
            applied.append(f"narrowed to supplier “{supplier}” on the {col} column")

    if date_from or date_to:
        col = next((c for c in _DATE_COLS if c in cols), None)
        if col:
            def within(r):
                d = date_svc.to_iso(r.get(col))
                if not d:
                    return False
                return (not date_from or d >= date_from) and (not date_to or d <= date_to)
            rows = [r for r in rows if within(r)]
            # said back in the house format — the filter compares ISO, the
            # sentence explaining it is read by a person
            span = " to ".join(date_svc.to_display(x) for x in (date_from, date_to) if x)
            applied.append(f"narrowed to {span} on the {col} column")

    if not applied:
        return rep, []
    note = rep.get("note") or ""
    extra = ("Rows were narrowed after the report ran, because this report has no "
             "filter for that. Totals below are sums of the columns shown over the "
             f"{len(rows)} remaining row(s) — not the report's own totals.")
    return {**rep, "rows": rows, "totals": _numeric_totals(cols, rows),
            "note": (note + "  " if note else "") + extra}, applied


# ---------------------------------------------------------------------------
#  The whole answer
# ---------------------------------------------------------------------------
def ask(db, question):
    """Read the question, run the report it names, and say what was done.

    Always returns a dict — an unroutable question comes back with report=None
    and a reading that says so, never an exception for the screen to translate."""
    question = (question or "").strip()
    if not question:
        return {"ok": False, "question": question,
                "interpretation": {"reading": "nothing was asked", "engine": "none"},
                "report": None}

    read = interpret(question)
    key = read.get("report_key") or NO_MATCH

    if key == NO_MATCH or key not in reports_svc.REPORTS:
        return {
            "ok": False, "question": question, "report": None,
            "interpretation": {
                "report_key": None, "report_name": None,
                "reading": read.get("reading") or "no report answers this question",
                "engine": read.get("engine"), "confidence": read.get("confidence", "low"),
                "degraded": read.get("degraded"),
                "applied": {}, "ignored": [], "narrowed": [],
            },
        }

    name, group, fn = reports_svc.REPORTS[key]
    accepts = set(reports_svc._params(fn))

    # Everything the question asked to filter by, normalised. Dates go back
    # through services/dates.py rather than being trusted as ISO because they
    # came from a language model.
    wanted = {
        "date_from": date_svc.to_iso(read.get("date_from")),
        "date_to": date_svc.to_iso(read.get("date_to")),
        "as_on": date_svc.to_iso(read.get("as_on")),
        "kind": (read.get("kind") or "").strip().lower() or None,
    }
    if wanted["kind"] not in (None,) + MOVEMENT_KINDS:
        wanted["kind"] = None

    supplier, supplier_name = _match_supplier(db, read.get("supplier_name"))
    product, product_name = _match_product(db, read.get("product_query"))
    if supplier:
        wanted["supplier_id"] = supplier.id
    if product:
        wanted["product_id"] = product.id

    # A single date used where the report only has a range, and vice versa —
    # asking "stock as on 5 August" of a range report should still narrow.
    if wanted["as_on"] and "as_on" not in accepts and not wanted["date_to"]:
        wanted["date_to"] = wanted["as_on"]

    applied = {k: v for k, v in wanted.items() if v is not None and k in accepts}
    leftover = {k: v for k, v in wanted.items() if v is not None and k not in accepts}

    # Same guard on the model path as in _offline: an as-on date that was applied
    # makes an identical one-day range redundant, not unhonoured.
    if applied.get("as_on"):
        for k in ("date_from", "date_to"):
            if leftover.get(k) == applied["as_on"]:
                leftover.pop(k)

    rep = reports_svc.run(db, key, **applied)
    if rep is None:                                  # cannot happen: key is enum-checked
        return {"ok": False, "question": question, "report": None,
                "interpretation": {"report_key": key, "report_name": name,
                                   "reading": "that report could not be run",
                                   "engine": read.get("engine"), "applied": {},
                                   "ignored": [], "narrowed": []}}

    # What the report itself could not honour, narrowed afterwards and said so.
    rep, narrowed = _narrow(
        rep,
        supplier=supplier_name if "supplier_id" in leftover else None,
        date_from=leftover.get("date_from"),
        date_to=leftover.get("date_to"),
    )

    ignored = []
    for k, v in leftover.items():
        if k == "supplier_id" and any("supplier" in n for n in narrowed):
            continue
        if k in ("date_from", "date_to") and any("narrowed to" in n for n in narrowed):
            continue
        shown = date_svc.to_display(v) if k in ("as_on", "date_from", "date_to") else v
        label = {"supplier_id": f"supplier “{supplier_name}”", "product_id": f"product “{product_name}”",
                 "kind": f"movement kind “{v}”", "as_on": f"as-on date {shown}",
                 "date_from": f"from {shown}", "date_to": f"to {shown}"}.get(k, f"{k}={v}")
        ignored.append(f"{label} — {name} has no filter for it and no column to narrow on")

    return {
        "ok": True, "question": question, "report": rep,
        "interpretation": {
            "report_key": key, "report_name": name, "group": group,
            "reading": read.get("reading") or f"{name}",
            "engine": read.get("engine"), "confidence": read.get("confidence", "medium"),
            "degraded": read.get("degraded"),
            "supplier": supplier_name, "product": product_name,
            # `applied` is exactly what the report ran with, so the screen can
            # hand the same values to the CSV export and get the same rows.
            "applied": applied,
            "narrowed": narrowed,
            "ignored": ignored,
        },
    }


def examples():
    """A few questions that work, for the screen to show. Teaching by example is
    the only thing that tells someone what a blank box will accept."""
    return [
        {"q": "what did we buy last month", "note": "purchase register, dated"},
        {"q": "how much do we owe suppliers", "note": "pending bills"},
        {"q": "stock value right now", "note": "stock on hand"},
        {"q": "shortages not yet claimed", "note": "GRN shortages"},
        {"q": "payments made in August", "note": "payment register"},
        {"q": "what is still in transit", "note": "pending inward"},
        {"q": "கடந்த மாதம் வாங்கிய பொருட்கள்", "note": "purchased items, last month"},
        {"q": "நிலுவை பாக்கி எவ்வளவு", "note": "outstanding to suppliers"},
        {"q": "இருப்பு மதிப்பு", "note": "stock value"},
    ]
