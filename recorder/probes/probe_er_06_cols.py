"""Per-column NULL and zero counts over the model population (read-only)."""
import os, sqlite3
import config
from probe_er_fam import FAMILIES, POP

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

cols = [c for cs in FAMILIES.values() for c in cs]
fam_of = {c: f for f, cs in FAMILIES.items() for c in cs}

sel = ", ".join(
    f"SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) n_{i}, "
    f"SUM(CASE WHEN {c}=0 THEN 1 ELSE 0 END) z_{i}, "
    f"COUNT(DISTINCT {c}) d_{i}"
    for i, c in enumerate(cols)
)
q = f"SELECT COUNT(*) N, {sel} FROM training_rows WHERE {POP}"
r = con.execute(q).fetchone()
N = r["N"]
print("population N =", N)
print()
out = []
for i, c in enumerate(cols):
    out.append((r[f"n_{i}"], r[f"z_{i}"], r[f"d_{i}"], c, fam_of[c]))

print("=== columns 100%% NULL in the population ===")
any100 = False
for n, z, d, c, f in out:
    if n == N:
        any100 = True
        print(f"  {c:34s} {f:24s} NULL {n}/{N}")
if not any100:
    print("  (none)")
print()
print("=== columns >=90%% NULL ===")
for n, z, d, c, f in sorted(out, reverse=True):
    if n >= 0.90 * N and n < N:
        print(f"  {c:34s} {f:24s} NULL {n}/{N} = {100.0*n/N:.1f}%  distinct_nonnull={d}")
print()
print("=== columns with distinct_nonnull <= 1 (constant or dead) ===")
for n, z, d, c, f in out:
    if d <= 1:
        print(f"  {c:34s} {f:24s} distinct={d} NULL={n}/{N} zeros={z}")
print()
print("=== full per-column table (NULL%, zero%, distinct) grouped by family ===")
for f in FAMILIES:
    print(f"-- {f}")
    for n, z, d, c, ff in out:
        if ff != f:
            continue
        print(f"   {c:34s} NULL {n:6d} ({100.0*n/N:5.1f}%)  =0 {z:6d} ({100.0*z/N:5.1f}%)  distinct {d}")
