"""The signed "who is signed in" header the /pos mount hands the shop.

The other half — reading it and signing the person in — is the shop's
(Textile Retail Shop/app/essa_sso.py). The two cannot share code: the shop is
imported with its package name swapped in and out (see pos_mount), so this is
the one function of it the warehouse needs, written again. The format is pinned
by backend/tools/pos_sso_test.py, which signs here and verifies there.
"""
import base64
import hashlib
import hmac
import json
import time


def sign(key, username, full_name, role, now=None):
    body = base64.urlsafe_b64encode(json.dumps({
        "u": username, "n": full_name or username, "r": role,
        "t": int(now if now is not None else time.time())}).encode()).decode().rstrip("=")
    return f"{body}.{hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()}"
