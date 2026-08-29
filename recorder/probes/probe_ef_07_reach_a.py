import os
import pickle
import sqlite3
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=180)

SRC = {
    "chain_ownership": ("token_holders", None),
    "onchain_conc": ("chain_concentration", None),
    "onchain_auth_sol": ("chain_authority", None),
    "onchain_contract_evm": ("evm_contract", None),
    "flow": ("token_flow", None),
    "market_snapshot": ("market_ticks", None),
    "token_static": ("token_static", None),
}

out = {}
for fam, (tab, _) in SRC.items():
    q = (f"SELECT token_address, network_id, "
         f"MIN(CAST(strftime('%s', recorded_at) AS INTEGER)) mn, COUNT(*) c "
         f"FROM {tab} GROUP BY 1,2")
    exact, lower = {}, {}
    n = 0
    for a, net, mn, c in con.execute(q):
        n += 1
        exact[(a, str(net))] = mn
        k = (str(a).lower(), str(net))
        if k not in lower or (mn is not None and mn < lower[k]):
            lower[k] = mn
    out[fam] = {"table": tab, "exact": exact, "lower": lower}
    print(f"{tab}: {n} distinct (address,network) groups")

with open(os.path.join(HERE, "probe_ef_src.pkl"), "wb") as fh:
    pickle.dump(out, fh)
con.close()
