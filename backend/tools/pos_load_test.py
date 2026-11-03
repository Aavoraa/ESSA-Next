"""The shop loads even when another instance is creating its tables at the same
moment — and a load that does fail is tried again rather than kept.

    python backend/tools/pos_load_test.py

The failure this pins, from a deployment: two serverless instances started
together on a Postgres database that lacked the CRM's new tables; one created
`customer_orders`, the other's CREATE TABLE died on pg_type_typname_nsp_index,
and that instance said "POS is not loaded" until it was recycled. The race is
simulated here — a create_all that loses once — because two instances and a
Postgres server are not something a test run has.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="essa-posload-"), "test.db").replace(os.sep, "/")

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from sqlalchemy.exc import IntegrityError, ProgrammingError    # noqa: E402

from backend.app import main, pos_mount                         # noqa: E402

bad = []


def eq(what, got, want):
    if got != want:
        bad.append(what)
        print("  FAIL  %s\n        got  %r\n        want %r" % (what, got, want))
    else:
        print("  ok    %s" % what)


class _Session:
    rolled_back = 0

    def rollback(self):
        _Session.rolled_back += 1


class _DB:
    """A db whose create_all fails the first `lose` times with `error`."""

    def __init__(self, lose, error):
        self.lose, self.error, self.calls, self.session = lose, error, 0, _Session()

    def create_all(self):
        self.calls += 1
        if self.calls <= self.lose:
            raise self.error


LOST = IntegrityError("CREATE TABLE shop.customer_orders", {}, Exception(
    'duplicate key value violates unique constraint "pg_type_typname_nsp_index" '
    "DETAIL: Key (typname, typnamespace)=(customer_orders, 17971) already exists."))
EXISTS = ProgrammingError("CREATE TABLE shop.leads", {}, Exception('relation "leads" already exists'))
OTHER = ProgrammingError("CREATE TABLE x", {}, Exception("permission denied for schema shop"))

print("\ncreating the tables while another instance does the same")
db = _DB(1, LOST)
pos_mount.create_all_racing(db)
eq("the lost race on the row type is looked at again", db.calls, 2)
db = _DB(2, EXISTS)
pos_mount.create_all_racing(db)
eq("…and so is 'relation already exists'", db.calls, 3)
db = _DB(1, OTHER)
try:
    pos_mount.create_all_racing(db)
    eq("any other error is not swallowed", "no error", "raised")
except ProgrammingError:
    eq("any other error is not swallowed", db.calls, 1)
db = _DB(99, LOST)
try:
    pos_mount.create_all_racing(db, attempts=3)
    eq("a race that never settles is reported, not looped on", "no error", "raised")
except IntegrityError:
    eq("a race that never settles is reported, not looped on", db.calls, 3)

print("\na load that failed is tried again")
calls = {"n": 0}
real = pos_mount.load_pos_app


class _Flask:
    def __call__(self, environ, start_response):          # pragma: no cover — never served
        return []


def flaky():
    calls["n"] += 1
    if calls["n"] == 1:
        raise RuntimeError("database briefly unreachable")
    return _Flask()


pos_mount.load_pos_app = flaky
main._pos_app, main._pos_loaded, main._pos_error, main._pos_failed_at = None, False, None, 0.0
eq("the first load fails and says why", (main._pos_asgi(), main._pos_error),
   (None, "RuntimeError: database briefly unreachable"))
eq("straight after, it is not hammered", (main._pos_asgi(), calls["n"]), (None, 1))
main._pos_failed_at -= main.POS_RETRY_SECONDS + 1                    # the wait has passed
eq("after the wait it loads", main._pos_asgi() is not None, True)
eq("and the error is cleared", main._pos_error, None)
eq("once loaded it is not loaded again", (main._pos_asgi() is not None, calls["n"]), (True, 2))
pos_mount.load_pos_app = real

print("\n%d FAILED" % len(bad) if bad else "\nall passing")
sys.exit(1 if bad else 0)
