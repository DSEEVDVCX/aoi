"""What would the age gate have done? Measured on real operational admissions."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

import config

MIN_DAYS = config.MIN_TOKEN_AGE_DAYS

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=30)
con.row_factory = sqlite3.Row

rows = con.execute(
    """SELECT w.token_address tok, w.network_id net, w.first_seen_at fs,
              t.token_created_at created
         FROM watch_windows w
         LEFT JOIN token_static t
           ON t.token_address=w.token_address AND t.network_id=w.network_id
        WHERE w.is_control=0 AND w.admission_source IS NULL"""
).fetchall()


def age_days(created, fs_iso):
    if created in (None, ""):
        return None
    try:
        c = int(float(created))
    except (TypeError, ValueError):
        return None
    if c <= 0:
        return None
    age = (int(datetime.fromisoformat(fs_iso).timestamp()) - c) / 86400.0
    return age if age >= 0 else None


ok = young = unknown = 0
for r in rows:
    a = age_days(r["created"], r["fs"])
    if a is None:
        unknown += 1
    elif a < MIN_DAYS:
        young += 1
    else:
        ok += 1

tot = len(rows) or 1
print(f"operational signal admissions in history: {len(rows)}")
print(f"  would be ADMITTED (>= {MIN_DAYS}d) : {ok:5}  ({100.0*ok/tot:.1f}%)")
print(f"  would be REJECTED (<  {MIN_DAYS}d) : {young:5}  ({100.0*young/tot:.1f}%)")
print(f"  would be REJECTED (unknown)     : {unknown:5}  ({100.0*unknown/tot:.1f}%)")
print(f"  -> total rejected               : {young+unknown:5}  "
      f"({100.0*(young+unknown)/tot:.1f}%)")

# Distinct coins, not windows -- a coin re-signalled later gets another chance.
d = con.execute(
    """SELECT COUNT(*) n FROM (
         SELECT w.token_address, w.network_id,
                MIN(CAST(t.token_created_at AS INTEGER)) c
           FROM watch_windows w JOIN token_static t
             ON t.token_address=w.token_address AND t.network_id=w.network_id
          WHERE w.is_control=0 AND w.admission_source IS NULL
          GROUP BY 1,2)"""
).fetchone()["n"]
print(f"\ndistinct coins ever admitted operationally: {d}")

# What the training set loses. `model_training_rows` already carries
# `token_age_h` -- the age at window start, in hours -- so read the feature
# instead of recomputing it. (There is no `window_start` column to join on.)
#
# But the view is NOT queried here: its two correlated dedup subqueries (the
# NOT EXISTS over signal_events and the MIN(key) per token/ts) make a full scan
# take minutes. Its cheap filters are applied to `training_rows` directly; the
# only thing skipped is duplicate-signal dedup, which is independent of age, so
# the percentages below hold for the view too.
t = con.execute(
    """SELECT COUNT(*) tot,
              SUM(CASE WHEN token_age_h IS NULL THEN 1 ELSE 0 END) unk,
              SUM(CASE WHEN token_age_h < ? THEN 1 ELSE 0 END) young
         FROM training_rows
        WHERE kind='signal' AND is_live=1 AND asset_class='meme'
          AND status='ok' AND is_independent=1
          AND feature_version = CAST(COALESCE(
              (SELECT value FROM meta WHERE key='current_feature_version'), '0'
          ) AS INTEGER)""",
    (MIN_DAYS * 24.0,),
).fetchone()
tt = t["tot"] or 1
kept = t["tot"] - t["young"] - t["unk"]
print(f"\ntraining rows (pre-dedup)          : {t['tot']}")
print(f"  age <  {MIN_DAYS}d (window never opens): {t['young']:5}  "
      f"({100.0*t['young']/tt:.1f}%)")
print(f"  age unknown                      : {t['unk']:5}  "
      f"({100.0*t['unk']/tt:.1f}%)")
print(f"  -> rows kept                     : {kept:5}  "
      f"({100.0*kept/tt:.1f}%)")
con.close()
