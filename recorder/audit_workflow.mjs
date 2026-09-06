export const meta = {
  name: 'recorder-data-audit',
  description: 'Audit recorder.db for missing data, all-zero/all-null feature columns, and collection gaps',
  phases: [
    { title: 'Audit', detail: 'six independent domain auditors over recorder.db' },
    { title: 'Verify', detail: 'adversarially re-check every finding with fresh SQL' },
    { title: 'Synthesize', detail: 'one prioritized report' },
  ],
}

const ENV = `
ENVIRONMENT (read carefully — you have no prior context):

- Project: C:\\Users\\rr\\Desktop\\aoi  — a FOMO crypto-signal recorder + ML training pipeline.
- ALWAYS cd to C:\\Users\\rr\\Desktop\\aoi\\recorder first. Python modules there import by bare name (import config, import db, import features).
- The database is recorder.db in that directory. A LIVE recorder process writes to it every 60 seconds.
  *** NEVER write to the database. NEVER run a migration, UPDATE, INSERT, DELETE, or VACUUM. ***
  *** NEVER run build_training_rows.py, labeler.py, recorder.py or any run_*.py — they open the DB read-write and would contend with the live recorder. ***
  Open it read-only, always:
      import os, sqlite3, config
      uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
      con = sqlite3.connect(uri, uri=True, timeout=120)
      con.row_factory = sqlite3.Row
- Write your queries into a .py file (use the Write tool) and run it with \`python <file>.py\`. Name any file you create probe_*.py so it is recognizable. Long multi-line \`python -c\` commands are being rejected by a flaky safety classifier in this environment, so use files.
- IMPORTANT: the Bash safety classifier is intermittently failing with "claude-opus-5 is temporarily unavailable". That is NOT your command being wrong. Retry the exact same command up to 10 times, doing other read-only work (Read/Grep/Glob need no classifier) in between. Do not give up and do not report it as a finding.

CRITICAL PERFORMANCE WARNING:
- \`model_training_rows\` is a VIEW containing two correlated subqueries (a NOT EXISTS self-join over signal_events and a MIN(key) per token/ts). A full scan of it takes MINUTES and may never finish. DO NOT query the view.
  Instead query the base table \`training_rows\` with the view's cheap filters:
      WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
        AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
  The only thing this skips is duplicate-signal dedup. Call this "the model population".
- Some tables are large (the DB is hundreds of MB). Prefer aggregate queries over row dumps. If a query runs longer than ~3 minutes, narrow it.

DOMAIN FACTS YOU MUST KNOW (they change what counts as a bug):
- The project's explicit doctrine, written throughout schema.sql in comments: an unmeasured value MUST be NULL, never 0 ("not measured" is not "zero"; an absent value never becomes zero). So a feature column full of 0 where 0 means "we did not collect it" is a REAL BUG. But a genuine measured zero (onchain_code_size=0 meaning "not a contract", a real count of 0 trades) is CORRECT and documented as such. Distinguish the two by reading the schema.sql comment above the column and the features.py code that writes it.
- Columns can legitimately be NULL for a documented reason — measured absence. Examples the schema documents: the top_trader_*_24h/7d/30d family is NULL in rows built before that collector was switched on; onchain_holder_count is NULL on Solana by design (getTokenLargestAccounts caps at 20 accounts and cannot know the total); onchain_owner_renounced NULL means "no owner function", different from 0; the EVM contract family (onchain_has_mint_fn etc.) is documented as meaningful on Base but near-constant on BSC and Robinhood Chain.
- ALREADY CHECKED, do not redo: (1) a static diff of features.ROW_COLUMNS against the training_rows columns in schema.sql is clean — every column matches; (2) build_training_rows.py inserts with \`{c: r.get(c) for c in cols}\`, so a missing key becomes NULL, not 0 — correct per doctrine; (3) no production code calls julianday() on token_created_at (features.py computes the age in Python via epoch_of, which handles both ISO strings and numeric epochs). What is NOT yet checked is whether the LIVE table has extra columns added by a migration that are absent from features.ROW_COLUMNS; such a column would be NULL forever while still passing the schema check. Verify that with PRAGMA table_info(training_rows) against features.ROW_COLUMNS.
- raw_json / *_json columns are zlib-compressed BLOBs. NEVER json.loads them. Decode with the module-level helper: \`import db as dbmod; dbmod.decode_raw(blob)\`.
- Two different holder counts exist and must not be conflated: holder_count / chain_holder_count is the whole chain (tens of thousands); platform_holders is FOMO users only (thousands).
- The signal population is ~45% Solana / ~55% EVM. networkId 1399811149 is Solana; 8453 is Base; 4663 is Robinhood Chain (~37% of signals). EVM addresses may appear in mixed case — case-insensitive comparison bugs are a real class here.
- Enrichment families (flow_*, holders/onchain, social/thesis) are known to be sparse at t=0 for three DIFFERENT reasons: some collectors run on a slower sweep than the 60s cycle (coverage = N/K minutes), some cover only one network, some only start after a coin joins the watchlist. Sparse is expected; report the MEASURED coverage and only call it a bug if it contradicts the documented design.
- A trigger signal now opens a 48h watch window only if the coin is >= 2 days old (config.MIN_TOKEN_AGE_DAYS = 2.0). That gate went live 2026-08-22; rows before that had no gate. The control arm (\`admit_control_sample\`) got the same gate later the same day — counters \`control_age_rejected\` / \`control_age_rejected_total\`.
- Training data policy: only live rows count (is_live=1). Retro/backfilled rows were deliberately rejected and deleted. Do not recommend backfilling history.
- ALREADY AUDITED AND RESOLVED 2026-08-22, do not re-report: (1) the 11 \`onchain_*\` EVM-contract columns were 100% NULL because \`EVM_CONTRACT_NETWORKS\` was Base-only; it is now ("8453","56","4663") and BSC/Robinhood rows exist from 21:40Z. (2) \`top10_holders_pct\` is retired from features.FEATURE_COLUMNS (upstream never sends the key) so features.ROW_COLUMNS is now 198 against 199 table columns — that ONE-column gap is deliberate and documented, not migration drift. (3) There are no all-empty rows: 0 of ~10,820 model rows have every non-trigger family NULL.
- Today is 2026-08-22.

WHAT YOU RETURN:
Findings only for things actually WRONG or MISSING, each backed by a number you measured yourself plus the exact SQL that produced it. Also return notable measurements even when they are not bugs — the user asked "is anything missing", so a clean coverage number is a useful answer. Be specific: "column X is NULL in 4,812 of 4,812 rows (100%)" not "some columns look empty". Never guess a number. If a query fails, list it in queries_that_failed rather than inventing a result.
`

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    domain: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string', description: 'one-line statement of the defect' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          evidence: { type: 'string', description: 'the numbers you measured, with counts and percentages' },
          sql: { type: 'string', description: 'the exact query that produced the evidence' },
          affected: { type: 'string', description: 'which columns / tables / rows, and how many' },
          why_it_matters: { type: 'string' },
          fix: { type: 'string', description: 'concrete suggested fix, or "unclear"' },
        },
        required: ['title', 'severity', 'evidence', 'sql', 'affected', 'why_it_matters'],
      },
    },
    measurements: {
      type: 'array',
      description: 'notable numbers that are NOT bugs but answer "is anything missing"',
      items: { type: 'string' },
    },
    queries_that_failed: { type: 'array', items: { type: 'string' } },
  },
  required: ['domain', 'findings', 'measurements'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    title: { type: 'string' },
    real: { type: 'boolean', description: 'false if you refuted it or could not reproduce the number' },
    reproduced_evidence: { type: 'string', description: 'the number YOU measured, independently' },
    note: { type: 'string', description: 'why it stands, or exactly how it is wrong' },
    severity_corrected: { type: 'string', enum: ['high', 'medium', 'low'] },
  },
  required: ['title', 'real', 'reproduced_evidence', 'note'],
}

const DOMAINS = [
  {
    key: 'column-health',
    prompt: `${ENV}

YOUR DOMAIN: per-column health of \`training_rows\` — the heart of the user's question ("there might be rows of zeros from which we haven't collected anything").

1. Get the column list with PRAGMA table_info(training_rows) (~200 columns). Diff it against features.ROW_COLUMNS and report any live column the builder never writes.
2. Generate ONE query in Python from that column list (do not hand-write 200 expressions) computing, for the model population, per column: total rows, NULL count, count of value = 0, COUNT(DISTINCT non-null), MIN, MAX.
3. Repeat over ALL of training_rows (every kind, every feature_version) so you can separate "dead everywhere" from "dead only in the current version".
4. Classify every column: (a) 100% NULL; (b) >95% NULL; (c) >90% exactly zero; (d) constant / one distinct value — useless as a feature; (e) healthy.
5. For every column in (a), (c), (d): read its schema.sql comment and the features.py code that computes it, and decide documented-measured-absence / genuine-zero versus real bug. Quote the comment or code line.
6. Report the FULL lists for (a), (c) and (d) — name every column, do not truncate.`,
  },
  {
    key: 'empty-rows',
    prompt: `${ENV}

YOUR DOMAIN: whole rows carrying (almost) no collected data.

1. Define feature FAMILIES by column-name prefix and by the schema.sql section comments: trigger-event, token-static, social/thesis, price-path (ret_*/bars_*/ath_*), market snapshot (tick_*/liquidity/holders/volume_*), chain ownership (chain_*/platform_*), onchain (onchain_*), flow (flow_*), market regime (sol_*/eth_*), signal density (prior_*/global_*), labels.
2. Per row in the model population, count how many families are entirely NULL. Report the distribution (rows with 0, 1, 2, ... empty families).
3. Find rows where EVERY feature family except the trigger event is NULL — admitted the coin, collected nothing. Count them and show a few keys.
4. Correlate emptiness with time: bucket by day of entry_ts and by built_at. Is it concentrated in specific periods (recorder downtime, an outage, a provider block)? Cross-check against the meta table and any cycle-history table you find.
5. Correlate emptiness with network_id (1399811149 vs 8453 vs 4663) and with is_live.
6. Report how many rows are usable in the strict sense: no empty family outside the ones documented as network-limited.`,
  },
  {
    key: 'collector-coverage',
    prompt: `${ENV}

YOUR DOMAIN: the collection layer — is every watched coin actually collected, and how fresh is it?

Discover real table names first: SELECT name FROM sqlite_master WHERE type='table'. Then per collector table (ticks, bars, token details / holders, social / thesis, flow, chain / onchain / concentration / authority, evm ledger, traders, macro bars):
1. Row count, distinct coins covered, earliest and latest recorded_at.
2. Coverage against the CURRENT active operational watchlist (watchlist WHERE active=1 AND is_control=0): how many active watches have zero rows there? Give the count and examples.
3. Coverage against every coin ever watched.
4. Staleness: distribution of minutes since the newest row per coin. Which collectors have coins whose newest data is hours or days old while the watch is still active?
5. Per-network coverage: does each collector cover Solana only, EVM only, or both? Measure, do not assume.
6. Continuity gaps: for the tick and bar collectors find time gaps > 10 minutes over the last 7 days, total them in minutes and as a percentage of the period. This is recorder downtime.
7. Read the meta table for every key matching last_error%, %streak%, %_last_ok_at, %_last_run_at and report anything indicating a stalled or silently-failing collector (a stale ok-stamp, a non-zero streak).`,
  },
  {
    key: 'outcomes-labels',
    prompt: `${ENV}

YOUR DOMAIN: outcomes and labels — the training target. A missing or wrong label silently poisons the model.

1. Find the outcomes and watch_windows tables. Report counts and join integrity between watch_windows, outcomes and training_rows on (kind, key).
2. How many closed windows (past 48h) have NO outcome row? How many have an outcome row with NULL final_return_48h or NULL is_rug?
3. In training_rows: rows where status='ok' but a label (final_return_48h, max_gain_48h, max_drawdown_48h, is_rug, time_to_peak_h) is NULL. status='ok' should imply a complete label — verify and report violations.
4. suspect_bars: how many rows are flagged, and is the flag excluded anywhere? Check whether the model view filters it (read schema.sql).
5. Sanity-check label VALUES: is_rug distribution; final_return_48h min/max/median (look for impossible values, exact -100%, +1e6); rows where max_gain_48h < final_return_48h; the sign convention of max_drawdown_48h (determine the intended sign by reading labeler.py, then find violations); time_to_peak_h outside [0,48].
6. Leakage the other way: rows whose window has NOT closed yet but which already carry labels.
7. Read labeler.py to learn intended semantics BEFORE calling any of the above a bug.`,
  },
  {
    key: 'static-write-paths',
    prompt: `${ENV}

YOUR DOMAIN: static audit of the write paths — columns that CANNOT be filled correctly, found by reading code. Read/Grep need no classifier, so you can work while Bash is flaky.

1. Hunt absent-becomes-zero coercions across features.py, recorder.py, extract.py, labeler.py, evm_layer.py, chain_layer.py and any other collector module: patterns like \`or 0\`, \`float(x or 0)\`, \`.get(k, 0)\`, \`COALESCE(...,0)\`, \`int(x or 0)\`, and \`sum()\` over an empty sequence (yields 0, not None). For each hit decide whether that 0 is a genuine measurement or a fabricated one, and quote file:line. (Note: a prior grep of features.py for the obvious patterns found only ONE hit, at features.py:340 — so search the OTHER files harder, and search subtler forms: \`if not x: x = 0\`, \`round(x or 0, 2)\`, default arguments of 0, dict.setdefault(k, 0), accumulators initialised to 0, and \`len(...)\` used where None was meant.)
2. The reverse error: a genuine measured zero stored as NULL (a real count of 0 turned into None), which makes real information look like absence.
3. Ratio / derived features — what happens when the denominator is 0 or NULL? Check size_to_mcap, buy_sell_ratio_24h, every flow_* ratio, platform_penetration, volume_to_liquidity, liquidity_to_mcap, float_ratio, social_holder_ratio, vol_surge_1h, top_trader_match_ratio, volume_per_trader. Quote the code for each and say what value lands in the DB.
4. Check every place a feature is derived from a nullable input without guarding: log_market_cap (log of 0 or None), price_to_avg_cost, size_to_mcap.
5. Read the epoch/format normalisation helper (features.epoch_of) and check every caller handles both an ISO string and a numeric epoch, and both seconds and milliseconds.
6. Verify each conclusion with a small read-only aggregate query where you can.`,
  },
  {
    key: 'integrity-consistency',
    prompt: `${ENV}

YOUR DOMAIN: relational integrity and cross-table consistency.

1. Orphans in both directions between signal_events, watchlist, watch_windows, token_static, outcomes, training_rows. E.g. watch_windows whose token has no token_static row; training_rows whose key has no signal_event; watchlist rows with no window.
2. Address and network normalization: are EVM addresses stored in mixed case anywhere? Find tokens appearing under two casings of the same address, or under both NULL and empty-string network_id, or as '1399811149' text vs 1399811149 integer (SQLite type affinity). Each silently splits one coin into two and breaks joins. Measure how many coins are affected, per table.
3. token_static: rows missing token_created_at, symbol, decimals, launchpad. Age coverage was repaired on 2026-08-22 — confirm it holds, both for active watches and for every coin ever watched.
4. Duplicates: duplicate signal_events for the same (token, ts, signal_type); duplicate watch_windows for one window; training_rows keys shared across kinds.
5. Timestamp sanity across tables: recorded_at in the future; entry_ts of 0 or negative; timezone-naive and timezone-aware ISO strings mixed in one column (this breaks string comparison and datetime.fromisoformat ordering); epoch seconds vs milliseconds confusion.
6. split / is_live / is_independent: verify split assignment is by token hash so one coin never lands in two splits — find any coin appearing in more than one split. Report the train/val/test balance and the is_independent funnel (how many rows survive each filter of the model view, measured cheaply one filter at a time).`,
  },
]

// `args` selects which domains to run: an array of keys, or a comma-separated
// string. Omit it to run all six. Used to re-run only the domains that died on
// API quota during the 2026-08-22 run.
const WANTED = (() => {
  if (!args) return null
  const list = Array.isArray(args) ? args : String(args).split(',')
  const keys = list.map((k) => String(k).trim()).filter(Boolean)
  return keys.length ? new Set(keys) : null
})()
const SELECTED = WANTED ? DOMAINS.filter((d) => WANTED.has(d.key)) : DOMAINS
if (WANTED) {
  const missing = [...WANTED].filter((k) => !DOMAINS.some((d) => d.key === k))
  if (missing.length) throw new Error(`unknown domain keys: ${missing.join(', ')}`)
  log(`scoped re-run: ${SELECTED.length} of ${DOMAINS.length} domains — ${SELECTED.map((d) => d.key).join(', ')}`)
}

phase('Audit')

const results = await pipeline(
  SELECTED,
  (d) => agent(d.prompt, {
    label: `audit:${d.key}`,
    phase: 'Audit',
    schema: FINDINGS_SCHEMA,
    effort: 'high',
  }),
  (report, d) => {
    if (!report || !report.findings || report.findings.length === 0) {
      return { report, judged: [] }
    }
    return parallel(report.findings.map((f) => () =>
      agent(`${ENV}

YOU ARE AN ADVERSARIAL VERIFIER. Another agent audited recorder.db and produced the finding below. Your job is to REFUTE it. Assume it is wrong until your own independent measurement forces you to agree. Default to real=false when you cannot reproduce the number.

CLAIM: ${f.title}
CLAIMED SEVERITY: ${f.severity}
CLAIMED EVIDENCE: ${f.evidence}
AFFECTED: ${f.affected}
THEIR SQL:
${f.sql}

Do this:
1. Re-measure independently. Write your OWN query — do not merely re-run theirs. If you also run theirs, say whether the two agree.
2. Check the three ways this kind of claim is usually wrong:
   a. The number is real but is NOT a defect — schema.sql or features.py explicitly documents it as measured absence, a genuine zero, or a by-design single-network limitation. Read the actual comment above the column and the code that writes it, and quote them.
   b. The population was wrong — it scanned all of training_rows including retro (is_live=0) or stale feature_version rows, or used the expensive view, or filtered the wrong column, inflating the percentage.
   c. The claim conflates two similar things — holder_count vs platform_holders, chain_ vs platform_ vs onchain_ families, or a collector that is one-network-by-design.
3. State the corrected severity. Downgrade freely: a 100%-NULL column that the schema documents as "NULL in rows built before this collector existed" is low, not high.
4. Set real=false if it is not a defect, if you could not reproduce the number, or if your number differs from the claim by more than 20% relative.`,
      { label: `verify:${d.key}`, phase: 'Verify', schema: VERDICT_SCHEMA, effort: 'high' },
    ).then((v) => ({ finding: f, verdict: v, domain: d.key })))).then((j) => ({ report, judged: j }))
  },
)

const clean = results.filter(Boolean)
const judged = clean.flatMap((r) => (r && r.judged) || []).filter((x) => x && x.verdict)
const confirmed = judged.filter((x) => x.verdict.real)
const refuted = judged.filter((x) => !x.verdict.real)
const measurements = clean.flatMap((r) => (r && r.report && r.report.measurements) || [])
const failures = clean.flatMap((r) => (r && r.report && r.report.queries_that_failed) || [])
log(`findings: ${judged.length} judged, ${confirmed.length} confirmed, ${refuted.length} refuted; ${measurements.length} measurements; ${failures.length} failed queries`)

phase('Synthesize')

const report = await agent(`${ENV}

You are writing the FINAL AUDIT REPORT for the owner of this pipeline. They asked, in their words: "check the data and see if there are any missing information or problems. There might be rows of zeros from which we haven't collected anything."

Six domain auditors ran, and every finding was then adversarially verified by a separate agent. Here is the verified material as JSON.

SCOPE OF THIS RUN: ${SELECTED.length} of ${DOMAINS.length} domains — ${SELECTED.map((d) => d.key).join(', ')}. ${SELECTED.length < DOMAINS.length ? 'This is a SCOPED RE-RUN of domains that failed in an earlier audit; the other domains are already reported elsewhere. Cover ONLY these domains and say so in the VERDICT, and do not restate findings from domains that did not run in this pass.' : ''}

CONFIRMED FINDINGS (survived refutation):
${JSON.stringify(confirmed.map((x) => ({ domain: x.domain, title: x.finding.title, severity: x.verdict.severity_corrected || x.finding.severity, auditor_evidence: x.finding.evidence, verifier_evidence: x.verdict.reproduced_evidence, verifier_note: x.verdict.note, affected: x.finding.affected, why: x.finding.why_it_matters, fix: x.finding.fix })), null, 1)}

REFUTED FINDINGS (do NOT report as problems; mention only where the refutation itself proves something is healthy):
${JSON.stringify(refuted.map((x) => ({ domain: x.domain, title: x.finding.title, why_refuted: x.verdict.note, verifier_evidence: x.verdict.reproduced_evidence })), null, 1)}

CLEAN MEASUREMENTS reported by the auditors:
${JSON.stringify(measurements, null, 1)}

QUERIES THAT FAILED (coverage gaps in this audit itself):
${JSON.stringify(failures, null, 1)}

Write a markdown report with exactly these sections:
1. VERDICT — three sentences: is the data sound, what is the single worst problem, how much of the training set is affected.
2. ANSWER TO THE ZEROS QUESTION — directly: are there rows or columns of zeros/nulls with nothing collected? Give counts. Name every 100%-NULL column and every constant column, and mark each as BUG or DOCUMENTED-ABSENCE.
3. PROBLEMS, ranked by severity — each with the measured number, what it costs, and the concrete fix. Merge duplicates found by several auditors.
4. WHAT IS HEALTHY — the coverage numbers that came back clean, so the owner knows what not to worry about.
5. BLIND SPOTS — anything this audit could not measure, including the failed queries above. Never present a gap as a clean result.
6. RECOMMENDED ORDER OF WORK — a short numbered list.

Rules: every claim carries a number. Where auditor and verifier numbers disagree, use the VERIFIER's. Invent nothing not present above. If the material is thin because queries failed, say so at the top instead of padding. Concise and concrete; no filler.

Return the report as markdown text.`,
  { label: 'synthesize', phase: 'Synthesize', effort: 'high' })

return {
  confirmed_count: confirmed.length,
  refuted_count: refuted.length,
  failed_queries: failures,
  report,
}
