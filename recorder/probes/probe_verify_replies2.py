import os, sqlite3, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

# Decode real thesis envelopes and count whether the `numReplies` KEY is present.
# If present-with-0 -> a stored 0 is a MEASURED zero (doctrine-compliant).
# If absent -> a stored 0 is fabricated (doctrine violation, claim correct).
import extract

rows = con.execute("""
    SELECT raw_json FROM token_social
    WHERE thesis_sampled > 0
    ORDER BY recorded_at DESC LIMIT 400
""").fetchall()

items_total = 0
key_present = 0
key_absent = 0
nonzero = 0
parentid_present = 0
parentid_nonempty = 0
val_types = {}
sample_keys = None

for r in rows:
    env = dbmod.decode_raw(r["raw_json"])
    for it in extract.unwrap_thesis(env):
        items_total += 1
        if sample_keys is None:
            sample_keys = sorted(it.keys())
        if "numReplies" in it:
            key_present += 1
            v = it["numReplies"]
            val_types[repr(v)] = val_types.get(repr(v), 0) + 1
            if v not in (0, None, "0"):
                nonzero += 1
        else:
            key_absent += 1
        if "parentId" in it:
            parentid_present += 1
            if it.get("parentId"):
                parentid_nonempty += 1

print("envelopes decoded:", len(rows))
print("thesis items seen:", items_total)
print("  numReplies KEY PRESENT :", key_present)
print("  numReplies KEY ABSENT  :", key_absent)
print("  numReplies non-zero    :", nonzero)
print("  observed values        :", val_types)
print("  parentId key present   :", parentid_present, " non-empty:", parentid_nonempty)
print("\nitem keys sample:", sample_keys)

# Contrast: the SAME loop over the SAME items produces a healthy likes sum.
likes_key_present = 0
likes_nonzero = 0
for r in rows[:400]:
    env = dbmod.decode_raw(r["raw_json"])
    for it in extract.unwrap_thesis(env):
        c = it.get("comment") if isinstance(it.get("comment"), dict) else {}
        if "numLikes" in c:
            likes_key_present += 1
            if c.get("numLikes"):
                likes_nonzero += 1
print("\nsame items, numLikes key present:", likes_key_present, " non-zero:", likes_nonzero)

# Is social_replies an orphan migration column absent from ROW_COLUMNS?
import features
live_cols = [c[1] for c in con.execute("PRAGMA table_info(training_rows)")]
print("\nsocial_replies in features.ROW_COLUMNS:", "social_replies" in features.ROW_COLUMNS)
print("social_replies in live training_rows  :", "social_replies" in live_cols)

con.close()
