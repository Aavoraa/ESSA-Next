"""Signing in with the Essa session — no second login inside the Essa web app.

Inside Essa the shop is shown in a frame, and the person in front of it has
already signed in to Essa. Asking them to sign in again, to the shop's own
accounts, is a second door to the same room. So the /pos mount (backend/app/
main.py, `_with_store_access`) reads the Essa session it already resolves for
Store access and hands the shop WHO it is, in a header:

    X-Essa-User: <base64url JSON {u, n, r, t}>.<hex HMAC-SHA256>

and the shop signs that person in to the shop account of the same username —
made on the spot the first time, with a role from their Essa role.

WHY IT CANNOT BE FORGED. The HMAC key is made fresh, in memory, by the mount
each time the server starts (backend/app/pos_mount.SSO_KEY) and given to the
shop as `ESSA_SSO_KEY`. It is never written anywhere. A shop run on its own has
no key, so it ignores the header entirely and its login screen works exactly as
it always did; and the mount strips any copy of the header a client sends before
adding its own. A signature is good for SSO_MAX_AGE seconds.

WHAT IT DOES NOT CHANGE. Who may open WHICH Store screen is still the Store
access header (app/modules.py) and the shop account's role. An existing shop
account keeps the role the shop gave it — the shop's managers decide that, not
the name on the door.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time

HEADER = "X-Essa-User"
SSO_MAX_AGE = 300
#: Set in the session when the person was signed in this way, so that a request
#: arriving WITHOUT an Essa session (signed out of Essa, or the tab left open)
#: signs them out of the shop too rather than leaving the till open.
SESSION_FLAG = "essa_sso"

#: Essa role -> the shop role an account made on first sign-in gets.
ROLE_FOR = {"superboss": "admin", "superadmin": "admin", "admin": "manager", "user": "cashier"}


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(key, username, full_name, role, now=None):
    """The header value for one person — used by the mount, and by the tests."""
    body = _b64(json.dumps({"u": username, "n": full_name or username, "r": role,
                            "t": int(now if now is not None else time.time())}).encode())
    mac = hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def verify(key, value, now=None):
    """{username, full_name, role} from a header value, or None if it is not one
    this server signed in the last SSO_MAX_AGE seconds."""
    if not key or not value or value.count(".") != 1:
        return None
    body, mac = value.split(".")
    want = hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, mac):
        return None
    try:
        data = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    age = (now if now is not None else time.time()) - int(data.get("t") or 0)
    if not data.get("u") or age < -60 or age > SSO_MAX_AGE:
        return None
    return {"username": str(data["u"])[:64], "full_name": str(data.get("n") or data["u"])[:128],
            "role": str(data.get("r") or "user")}


def account_for(db, User, who):
    """The shop account for this Essa person — theirs by username, or a new one.
    None for a shop account somebody switched off: the shop's word stands."""
    user = User.query.filter_by(username=who["username"]).first()
    if user is not None:
        return user if user.active is not False else None
    user = User(username=who["username"], full_name=who["full_name"],
                role=ROLE_FOR.get(who["role"], "cashier"), active=True)
    # Never used to sign in — this account is entered through Essa — but the
    # column is required, so it holds something nobody knows.
    user.set_password(secrets.token_urlsafe(32))
    db.session.add(user)
    db.session.commit()
    return user
