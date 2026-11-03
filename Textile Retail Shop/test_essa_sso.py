"""Checks that inside Essa the shop needs no second login — and that nothing else
can use that door.

    python test_essa_sso.py

No pytest — the shop has no test dependency and this needs none. Runs against a
throwaway database. The mount's half (signing) is checked in
backend/tools/pos_sso_test.py; this is the shop's half: what a signed header
does, and everything that must NOT sign anybody in.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SHOP_DIR = Path(__file__).resolve().parent
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp()) / 'sso_test.db'}"
os.environ["ESSA_WAREHOUSE_DB"] = str(Path(tempfile.mkdtemp()) / "no-warehouse.db")
sys.path.insert(0, str(SHOP_DIR))

from app import create_app, db, essa_sso                  # noqa: E402
from app.models import Attendance, User                   # noqa: E402

KEY = "k" * 64
app = create_app()
failures = []


def check(name, got, want):
    ok_ = got == want
    print(f"{'PASS' if ok_ else 'FAIL'}  {name}")
    if not ok_:
        print(f"        got : {got!r}")
        print(f"        want: {want!r}")
        failures.append(name)


with app.app_context():
    db.create_all()
    owner = User(username="admin", full_name="Shop Owner", role="admin")
    owner.set_password("admin123")
    gone = User(username="left", full_name="Left Us", role="cashier", active=False)
    gone.set_password("x")
    db.session.add_all([owner, gone])
    db.session.commit()


def header(username, role="superadmin", key=KEY, now=None, name=None):
    return {essa_sso.HEADER: essa_sso.sign(key, username, name or username.title(), role, now=now)}


def signed_in(client, headers=None):
    """Whether this request is let in rather than sent to the login form."""
    return client.get("/customers/", headers=headers or {}).status_code == 200


def user(username):
    with app.app_context():
        return User.query.filter_by(username=username).first()


print("-- run on its own, the shop ignores the header and keeps its login")
c = app.test_client()
check("no key: a perfectly signed header signs nobody in", signed_in(c, header("superadmin")), False)

app.config["ESSA_SSO_KEY"] = KEY
print("\n-- mounted, the Essa user is signed in")
c = app.test_client()
check("signed in without the login form", signed_in(c, header("superadmin", name="Super Admin")), True)
u = user("superadmin")
check("a shop account was made for them", (u.full_name, u.role), ("Super Admin", "admin"))
with app.app_context():
    check("and checked in, as the login form does", Attendance.query.filter_by(user_id=u.id).count(), 1)
check("the login page sends them on", c.get("/login", headers=header("superadmin")).status_code, 302)

print("\n-- roles for a new account come from the Essa role")
for essa_role, shop_role in (("superboss", "admin"), ("admin", "manager"), ("user", "cashier")):
    name = f"p_{essa_role}"
    signed_in(app.test_client(), header(name, essa_role))
    check(f"Essa {essa_role} -> shop {shop_role}", user(name).role, shop_role)

print("\n-- an existing shop account is used as it is")
c = app.test_client()
check("Essa 'admin' is the shop's own admin", signed_in(c, header("admin", "user")), True)
check("…keeping the role the shop gave it", user("admin").role, "admin")
check("…and its password still works on its own", user("admin").check_password("admin123"), True)
check("an account the shop switched off is not let in", signed_in(app.test_client(), header("left")), False)

print("\n-- nothing else opens the door")
check("a header signed with another key", signed_in(app.test_client(), header("x1", key="z" * 64)), False)
check("an old header", signed_in(app.test_client(), header("x2", now=time.time() - 3600)), False)
v = essa_sso.sign(KEY, "x3", "X3", "superadmin")
body, mac = v.split(".")
check("a tampered one", signed_in(app.test_client(), {essa_sso.HEADER: body[:-2] + "AA." + mac}), False)
check("garbage", signed_in(app.test_client(), {essa_sso.HEADER: "not-a-header"}), False)
check("none of those made an account", [n for n in ("x1", "x2", "x3") if user(n)], [])

print("\n-- leaving Essa leaves the till; a different Essa user is that user")
c = app.test_client()
signed_in(c, header("superadmin"))
check("the next request without an Essa session is signed out", signed_in(c), False)
c = app.test_client()
signed_in(c, header("superadmin"))
signed_in(c, header("p_admin", "admin"))
with c.session_transaction() as s:
    check("another Essa user on the same browser becomes that user", s.get("_user_id"), str(user("p_admin").id))
c = app.test_client()
c.post("/login", data={"username": "admin", "password": "admin123"})
check("someone who signed in with the shop's own form stays signed in", signed_in(c), True)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all Essa sign-in checks passed")
