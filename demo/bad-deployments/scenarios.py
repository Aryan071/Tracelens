#!/usr/bin/env python3
"""Scripted "bad deployments" for the demo app, applied as real git commits in a SCRATCH clone.

Each scenario is a list of exact-string edits to the demo source. An edit whose anchor text is not
found exactly once raises: the scenarios must be updated when the demo changes, never silently skip.
The commits are mixed with decoys (formatting-only, comment-only, unrelated edits) so ranking the
guilty commit first is a real test, not a lookup.

  python3 demo/bad-deployments/scenarios.py build <source-repo> <dest-dir> [--snapshot]
      # prints the commit shas as JSON; --snapshot starts from the working tree's demo/ (uncommitted)

Scenarios: a synchronous call in the hot path and N+1 queries (Phase 4: application code), a missing
index (a migration that drops it: no application symbol changes) and a thrown exception (Phase 5).
An edit whose anchor (`old`) is empty creates a new file, which must not exist yet.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ORDERS = "demo/src/orders.ts"
Edit = tuple[str, str, str]  # (file, old text that must occur exactly once, replacement)

FINGERPRINT_FN = """/** Content fingerprint attached to audit logs. */
function fingerprintPayload(userId: number, sku: string, qty: number): string {
  const until = Date.now() + 25; // synchronous work on the event loop
  let h = 0;
  while (Date.now() < until) h = (h * 31 + userId + qty + sku.length) | 0;
  return String(h);
}

"""

SCENARIOS: dict[str, tuple[str, list[Edit]]] = {
    # ---- decoys: none of these may be blamed for a latency regression ----
    "decoy_db_retries": (
        "Raise DB wait retries for slow CI",
        [("demo/src/db.ts", "waitForDb(retries = 30)", "waitForDb(retries = 60)")],
    ),
    "decoy_db_retries_again": (
        "Tune DB wait retries",
        [("demo/src/db.ts", "waitForDb(retries = 60)", "waitForDb(retries = 90)")],
    ),
    "decoy_format_insert": (  # whitespace only: the symbol's token hash must not change
        "Reformat insertOrder query",
        [
            (
                ORDERS,
                '    const r = await pool.query("INSERT INTO orders.orders(user_id, sku, qty) VALUES ($1, $2, $3) RETURNING id", [userId, sku, qty]);',
                '    const r = await pool.query(\n      "INSERT INTO orders.orders(user_id, sku, qty) VALUES ($1, $2, $3) RETURNING id",\n      [userId, sku, qty]\n    );',
            )
        ],
    ),
    "decoy_new_helper": (
        "Add formatCents helper",
        [
            (
                ORDERS,
                "export async function createOrder(",
                "export function formatCents(cents: number): string {\n  return (cents / 100).toFixed(2);\n}\n\nexport async function createOrder(",
            )
        ],
    ),
    "decoy_comment_reserve": (  # comment only: no symbol change
        "Document reserveStock",
        [
            (
                "demo/src/inventory.ts",
                '    const r = await pool.query(\n      "UPDATE inventory.stock',
                '    // atomically decrement stock; fails (rowCount 0) when there is not enough\n    const r = await pool.query(\n      "UPDATE inventory.stock',
            )
        ],
    ),
    "decoy_new_fn_payments": (
        "Add maskCard helper",
        [
            (
                "demo/src/payments.ts",
                'app.post("/charge"',
                'export function maskCard(last4: string): string {\n  return "**** " + last4;\n}\n\napp.post("/charge"',
            )
        ],
    ),
    "decoy_db_retries_3": (
        "Tune DB wait retries once more",
        [("demo/src/db.ts", "waitForDb(retries = 90)", "waitForDb(retries = 120)")],
    ),
    "decoy_comment_reserve_2": (  # comment only
        "Document createOrder",
        [
            (
                ORDERS,
                "export async function createOrder(",
                "// Creates an order: reserve stock, insert the row, charge the card.\nexport async function createOrder(",
            )
        ],
    ),
    "decoy_new_fn_inventory": (
        "Add formatSku helper",
        [
            (
                "demo/src/inventory.ts",
                'app.post("/reserve"',
                'export function formatSku(n: number): string {\n  return "sku-" + n;\n}\n\napp.post("/reserve"',
            )
        ],
    ),
    # ---- guilty commits ----
    "guilty_sync_call": (
        "Add payload fingerprint for audit logging",
        [
            (
                ORDERS,
                "export async function createOrder(",
                FINGERPRINT_FN + "export async function createOrder(",
            ),
            (
                ORDERS,
                "    const reserve = await post(",
                "    fingerprintPayload(userId, sku, qty);\n    const reserve = await post(",
            ),
        ],
    ),
    "guilty_n_plus_one": (
        "Hydrate user orders with full details",
        [
            (
                ORDERS,
                "    return r.rows;\n",
                "    const detailed = [];\n    for (const row of r.rows) {\n"
                '      const d = await pool.query("SELECT id, sku, qty, created_at FROM orders.orders WHERE id = $1", [row.id]);\n'
                "      detailed.push(d.rows[0]);\n    }\n    return detailed;\n",
            )
        ],
    ),
    # Phase 5. No application symbol changes: only a migration (deploy_demo.sh applies new ones).
    "guilty_missing_index": (
        "Drop unused orders user_id index",
        [
            (
                "demo/db/migrations/002_drop_orders_user_idx.sql",
                "",
                "-- The index is only used by the order history page, which is rarely opened.\n"
                "-- Dropping it makes inserts cheaper.\n"
                "DROP INDEX orders.orders_user_id_idx;\n",
            )
        ],
    ),
    # A validation that throws, plus the error handling a developer would add so the process
    # survives it (Express 4 does not catch rejected promises): the handler answers 500.
    "guilty_throw": (
        "Reject orders above the payment limit",
        [
            (
                ORDERS,
                '  return traced("createOrder", FILE, async () => {\n',
                '  return traced("createOrder", FILE, async () => {\n'
                "    const totalCents = qty * 1999;\n"
                "    if (totalCents > 5000) throw new Error(`order total ${totalCents} exceeds the payment limit`);\n",
            ),
            (
                ORDERS,
                "  const out = await createOrder(userId, sku, qty);\n  res.status(out.status).json(out.body);\n",
                "  try {\n    const out = await createOrder(userId, sku, qty);\n    res.status(out.status).json(out.body);\n"
                '  } catch {\n    res.status(500).json({ error: "internal error" });\n  }\n',
            ),
        ],
    ),
}

# (scenario, label): the history that gets built, oldest first. Labels mark the commits tests care about.
HISTORY = [
    ("decoy_db_retries", None),
    ("decoy_format_insert", None),
    ("decoy_new_helper", None),
    ("guilty_sync_call", "guilty1"),
    ("decoy_comment_reserve", "tip1"),
    ("decoy_new_fn_payments", None),
    ("guilty_n_plus_one", "guilty2"),
    ("decoy_db_retries_again", "tip2"),
    ("decoy_new_fn_inventory", None),
    ("guilty_missing_index", "guilty3"),
    ("decoy_db_retries_3", "tip3"),
    ("guilty_throw", "guilty4"),
    ("decoy_comment_reserve_2", "tip4"),
]


def apply(name: str, root: Path) -> str:
    subject, edits = SCENARIOS[name]
    for rel, old, new in edits:
        path = root / rel
        if old == "":
            if path.exists():
                raise RuntimeError(f"{name}: {rel} already exists")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new)
            continue
        text = path.read_text()
        if text.count(old) != 1:
            raise RuntimeError(
                f"{name}: anchor found {text.count(old)}x (need exactly 1) in {rel}: {old[:70]!r}"
            )
        path.write_text(text.replace(old, new))
    return subject


def git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Ada Dev",
        "GIT_AUTHOR_EMAIL": "ada@example.com",
        "GIT_COMMITTER_NAME": "Ada Dev",
        "GIT_COMMITTER_EMAIL": "ada@example.com",
    }
    p = subprocess.run(
        ["git", "-C", str(root), *args], env=env, capture_output=True, text=True, check=False
    )
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.strip()


def snapshot_demo(source: Path, dest: Path) -> None:
    """Make `dest`'s demo/ equal to the working tree's, as one commit (for uncommitted demo edits)."""
    shutil.rmtree(dest / "demo")
    shutil.copytree(
        source / "demo", dest / "demo", ignore=shutil.ignore_patterns("node_modules", "dist")
    )
    git(dest, "add", "-A")
    if git(dest, "status", "--porcelain"):
        git(dest, "commit", "-q", "-m", "Snapshot of the working tree demo")


def build(source: Path, dest: Path, snapshot: bool = False) -> dict[str, str]:
    if dest.exists():
        raise RuntimeError(f"{dest} already exists")
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(dest)], check=True
    )
    if snapshot:
        snapshot_demo(source, dest)
    shas = {"base": git(dest, "rev-parse", "HEAD")}
    for name, label in HISTORY:
        subject = apply(name, dest)
        git(dest, "add", "-A")
        git(dest, "commit", "-q", "-m", subject)
        sha = git(dest, "rev-parse", "HEAD")
        shas[name] = sha
        if label:
            shas[label] = sha
    return shas


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--snapshot"]
    if len(args) != 3 or args[0] != "build":
        sys.exit(__doc__)
    print(json.dumps(build(Path(args[1]), Path(args[2]), "--snapshot" in sys.argv), indent=1))
