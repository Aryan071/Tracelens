#!/usr/bin/env python3
"""Scripted "bad deployments" for the demo app, applied as real git commits in a SCRATCH clone.

Each scenario is a list of exact-string edits to the demo source. An edit whose anchor text is not
found exactly once raises: the scenarios must be updated when the demo changes, never silently skip.
The commits are mixed with decoys (formatting-only, comment-only, unrelated edits) so ranking the
guilty commit first is a real test, not a lookup.

  python3 demo/bad-deployments/scenarios.py build <source-repo> <dest-dir>   # prints the commit shas as JSON

Scenarios implemented here change application code (sync call in the hot path, N+1 queries). The
missing-index and thrown-exception regressions need SQL/error correlation and arrive in Phase 5.
"""

from __future__ import annotations

import json
import os
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
]


def apply(name: str, root: Path) -> str:
    subject, edits = SCENARIOS[name]
    for rel, old, new in edits:
        path = root / rel
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


def build(source: Path, dest: Path) -> dict[str, str]:
    if dest.exists():
        raise RuntimeError(f"{dest} already exists")
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(dest)], check=True
    )
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
    if len(sys.argv) != 4 or sys.argv[1] != "build":
        sys.exit(__doc__)
    print(json.dumps(build(Path(sys.argv[2]), Path(sys.argv[3])), indent=1))
