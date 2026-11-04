"""Adding a column another instance has just added is not an error.

    python test_dbpatch_race.py

Several instances start together on serverless, and each runs dbpatch. One adds
the column; the others, having looked a moment earlier, try too. That must end
with the column there and the shop running — not with "duplicate column". The
race is simulated: the first look is told the column is missing when it is not.
"""
import os
import sys
import tempfile
from pathlib import Path

SHOP_DIR = Path(__file__).resolve().parent
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp()) / 'race.db'}"
os.environ["ESSA_WAREHOUSE_DB"] = str(Path(tempfile.mkdtemp()) / "no-warehouse.db")
sys.path.insert(0, str(SHOP_DIR))

from app import create_app, db, dbpatch                   # noqa: E402

app = create_app()
failures = []


def check(name, got, want):
    print(f"{'PASS' if got == want else 'FAIL'}  {name}")
    if got != want:
        print(f"        got : {got!r}\n        want: {want!r}")
        failures.append(name)


with app.app_context():
    db.create_all()
    real = dbpatch.table_columns
    looks = {"n": 0}

    def stale(table):
        """The first look misses `code`, as if read just before another instance added it."""
        looks["n"] += 1
        cols = real(table)
        return cols - {"code"} if looks["n"] == 1 else cols

    dbpatch.table_columns = stale
    check("losing the race to add a column is not an error",
          dbpatch.ensure_column("customers", "code", "VARCHAR(32)"), False)
    dbpatch.table_columns = real
    check("and the column is there", "code" in real("customers"), True)
    try:
        dbpatch.table_columns = lambda t: {"id"}         # a column that truly cannot be added
        dbpatch.ensure_column("customers", "bad name", "NOT A TYPE")
        check("a real failure is still raised", "no error", "raised")
    except Exception:                                   # noqa: BLE001
        check("a real failure is still raised", True, True)
    finally:
        dbpatch.table_columns = real

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all dbpatch race checks passed")
