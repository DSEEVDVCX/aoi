# API probe archive

These scripts are dated field measurements used to pin down the shape of fomo.family
endpoints and the behavior of pagination and candles; they are not runtime code and
not automated tests.

- They are not imported by `fomo_api`, and no Windows task runs them.
- Some of them make real calls and read the Privy credential from the file; do not
  run them unintentionally.
- Any read of `recorder.db` here must stay through a SQLite URI with `mode=ro`.
- If a probe is revived: move it into a named tool under `api/scripts/`, add a test
  or a verification report for it, and do not return it to the root of `api/` under
  a `_probe_*.py` name.
