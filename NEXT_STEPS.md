# OrderFlow AI — where things stand (14 Nov 2026)

## What is already done

* **Report delivered.** 109 pages, all ten chapters, every number measured.
  Only your name/USN and the six screenshots are missing.
* **Benchmark complete.** qwen3:8b over all 270 utterances:
  **0.904 intent accuracy, 0.903 macro-F1**, zero parse failures,
  part-name extraction perfect (122/122). Evidence preserved in
  `benchmark_runs/run1_270_prompt_v1/`.
* **Prompt bug fixed** in `app.py` (the read-vs-write defect: "what's the
  status of order 14" was being classified as a command to *change* the
  status). Revert with `git checkout app.py` if you ever want the old one.
* **34/34 unit tests pass** (`python -m pytest test_orderflow.py -v`).

## What is left — none of it is required to submit

### 1. ~~Fix verification~~ — DONE, and it worked

58/58 correct. query_order recall 0.577 -> 1.000, update_status 0.938 -> 1.000,
13 errors -> 0, no regression. Evidence in
`benchmark_runs/run2_58_prompt_v2_fixcheck/`. Written up as Section 7.4.2.

### 2. Optional: re-measure the full corpus with the fixed prompt (~92 min)

    python eval_parser.py

Expected ceiling is about 0.934 if the eight read-vs-write errors resolve
and nothing else regresses. The report currently states plainly that 0.904
is the PRE-FIX figure, so this is not needed for consistency — it would
just let you report a better number and show a before/after.

### 3. Optional: bound the run-to-run variance (~15 min per pass)

    python eval_parser.py --corpus orderflow_corpus_sample.json

45 stratified items. Run it 2-3 times and compare `intent_accuracy`.
Section 7.13 currently records that this was not measured.

## Important

`eval_parser.py` OVERWRITES `eval_results.json` every run. The original
270-item evidence is safe in `benchmark_runs/run1_270_prompt_v1/` — copy any
new run you want to keep into its own folder before running again.

Send me `eval_results.json` from any run and I will fold it into the report.
