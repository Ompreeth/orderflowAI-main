# -*- coding: utf-8 -*-
"""
OrderFlow AI — zero-shot parser benchmark.

Measures the LIVE Qwen2.5 intent parser (classify_intent in app.py) against the
same 270-utterance labelled corpus used for the supervised baselines, and
separately measures the deterministic keyword fallback (answer_report_query).

Run this from the project folder, with Ollama up:

    python eval_parser.py                 # one pass
    python eval_parser.py --runs 3        # three passes, reports run-to-run variance
    python eval_parser.py --limit 40      # quick smoke test on the first 40 items

Writes eval_results.json, eval_per_item.csv and eval_confusion_matrix.csv.
Nothing in app.py is modified or needs modifying: init_db(), bootstrap_admin()
and start_backup_scheduler() all sit inside app.py's __main__ guard, so
importing it is side-effect free.
"""
import argparse
import csv
import json
import os
import statistics as st
import sys
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from app import classify_intent, OLLAMA_MODEL, OLLAMA_URL
except Exception as exc:
    sys.exit("Could not import the parser from app.py: %s\n"
             "Run this script from the project folder (the one containing app.py)." % exc)

try:
    from app import answer_report_query
except Exception:
    answer_report_query = None

INTENTS = ["create_order", "update_status", "query_order", "list_orders", "log_quality",
           "list_inventory", "query_inventory", "consume_stock", "report_query"]

STATUS_CANON = {"received": "Received", "in review": "In Review", "inreview": "In Review",
                "review": "In Review", "accepted": "Accepted", "accept": "Accepted",
                "approve": "Accepted", "approved": "Accepted", "done": "Accepted",
                "complete": "Accepted", "completed": "Accepted"}


def norm_part(value):
    """Loose part comparison: the parser is told to echo the user's wording, so
    'steel rod' and 'Steel Rods' both count as hitting 'Steel Rods'. Scored on
    token overlap rather than string equality, mirroring _correct_part_name."""
    if not value:
        return set()
    return {t.rstrip("s") for t in str(value).lower().replace("-", " ").split() if t.isalpha()}


def part_ok(gold, got):
    if gold is None:
        return got in (None, "", "null")
    g, p = norm_part(gold), norm_part(got)
    return bool(g & p)


def num_ok(gold, got):
    if gold is None:
        return got in (None, "", "null")
    try:
        return int(float(str(got))) == int(gold)
    except (TypeError, ValueError):
        return False


def status_ok(gold, got):
    if gold is None:
        return True            # auto-advance utterances: any or no status is acceptable
    if not got:
        return False
    return STATUS_CANON.get(str(got).strip().lower(), str(got).strip()) == gold


def one_pass(corpus, verbose=True):
    rows, lat = [], []
    for n, item in enumerate(corpus, 1):
        t0 = time.perf_counter()
        try:
            parsed = classify_intent(item["text"]) or {}
            err = ""
        except Exception as exc:                      # Ollama down mid-run
            parsed, err = {}, "%s: %s" % (type(exc).__name__, exc)
        ms = (time.perf_counter() - t0) * 1000
        lat.append(ms)

        pred = (parsed.get("intent") or "unknown").strip()
        rows.append({
            "id": item["id"], "text": item["text"], "lang": item["lang"],
            "gold_intent": item["intent"], "pred_intent": pred,
            "intent_ok": pred == item["intent"],
            "part_ok": part_ok(item["part_name"], parsed.get("part_name")),
            "qty_ok": num_ok(item["quantity"], parsed.get("quantity")),
            "oid_ok": num_ok(item["order_id"], parsed.get("order_id")),
            "status_ok": status_ok(item["new_status"], parsed.get("new_status")),
            "deadline_ok": bool(parsed.get("deadline")) == bool(item["has_deadline"]),
            "latency_ms": round(ms, 1), "error": err,
        })
        if verbose and n % 10 == 0:
            acc = sum(r["intent_ok"] for r in rows) / len(rows)
            print("  %3d/%d   running accuracy %.3f   last %6.0f ms"
                  % (n, len(corpus), acc, ms), flush=True)
    return rows, lat


def prf(rows):
    out = {}
    for lab in INTENTS:
        tp = sum(1 for r in rows if r["gold_intent"] == lab and r["pred_intent"] == lab)
        fp = sum(1 for r in rows if r["gold_intent"] != lab and r["pred_intent"] == lab)
        fn = sum(1 for r in rows if r["gold_intent"] == lab and r["pred_intent"] != lab)
        p = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f = 2 * p * rc / (p + rc) if p + rc else 0.0
        out[lab] = {"precision": round(p, 4), "recall": round(rc, 4),
                    "f1": round(f, 4), "support": tp + fn}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1, help="repeat the whole pass N times")
    ap.add_argument("--limit", type=int, default=0, help="only the first N utterances")
    ap.add_argument("--corpus", default=os.path.join(HERE, "orderflow_corpus.json"))
    args = ap.parse_args()

    if not os.path.exists(args.corpus):
        sys.exit("Corpus not found at %s" % args.corpus)
    corpus = json.load(open(args.corpus, encoding="utf-8"))
    if args.limit:
        corpus = corpus[:args.limit]

    print("OrderFlow AI parser benchmark")
    print("  model   : %s" % OLLAMA_MODEL)
    print("  endpoint: %s" % OLLAMA_URL)
    print("  items   : %d   runs: %d\n" % (len(corpus), args.runs))

    all_runs, accs = [], []
    for run in range(1, args.runs + 1):
        print("run %d/%d" % (run, args.runs), flush=True)
        t0 = time.time()
        rows, lat = one_pass(corpus)
        acc = sum(r["intent_ok"] for r in rows) / len(rows)
        accs.append(acc)
        all_runs.append({"run": run, "rows": rows, "latency": lat})
        print("  -> accuracy %.4f in %.0fs\n" % (acc, time.time() - t0), flush=True)

    last = all_runs[-1]["rows"]
    lat = all_runs[-1]["latency"]
    srt = sorted(lat)

    scored = [r for r in last if not r["error"]]
    def slot(name, subset=None):
        pool = subset if subset is not None else scored
        return round(sum(r[name] for r in pool) / len(pool), 4) if pool else None

    with_part = [r for r in scored if r["gold_intent"] in
                 ("create_order", "query_inventory", "consume_stock")]
    with_oid = [r for r in scored if r["gold_intent"] in
                ("update_status", "query_order", "log_quality")]
    with_status = [r for r in scored if r["gold_intent"] == "update_status"]

    by_lang = defaultdict(list)
    for r in scored:
        by_lang[r["lang"]].append(r["intent_ok"])

    results = {
        "model": OLLAMA_MODEL, "endpoint": OLLAMA_URL,
        "n_items": len(corpus), "runs": args.runs,
        "errors": sum(1 for r in last if r["error"]),
        "intent_accuracy": round(sum(r["intent_ok"] for r in last) / len(last), 4),
        "intent_accuracy_per_run": [round(a, 4) for a in accs],
        "intent_accuracy_stdev": round(st.pstdev(accs), 4) if len(accs) > 1 else 0.0,
        "macro_f1": round(sum(v["f1"] for v in prf(last).values()) / len(INTENTS), 4),
        "per_intent": prf(last),
        "accuracy_by_language": {k: round(sum(v) / len(v), 4) for k, v in sorted(by_lang.items())},
        "slot_accuracy": {
            "part_name (order/stock intents)": slot("part_ok", with_part),
            "quantity": slot("qty_ok"),
            "order_id (order-referencing intents)": slot("oid_ok", with_oid),
            "new_status (update_status only)": slot("status_ok", with_status),
            "deadline_presence": slot("deadline_ok"),
        },
        "latency_ms": {
            "mean": round(st.mean(lat), 1), "median": round(st.median(lat), 1),
            "p95": round(srt[int(0.95 * len(srt)) - 1], 1),
            "min": round(min(lat), 1), "max": round(max(lat), 1),
            "total_seconds": round(sum(lat) / 1000, 1),
        },
    }

    # the deterministic fallback, on the same report_query items
    if answer_report_query is not None:
        rq = [c for c in corpus if c["intent"] == "report_query" and c["lang"] == "en"]
        hit, dberr = 0, None
        for c in rq:
            try:
                if answer_report_query(c["text"]):
                    hit += 1
            except Exception as exc:
                dberr = "%s: %s" % (type(exc).__name__, exc)
                break
        results["rule_fallback"] = {
            "english_report_queries": len(rq), "answered": hit,
            "coverage_pct": round(100 * hit / len(rq), 1) if rq and not dberr else None,
            "database_error": dberr,
        }

    cm = Counter((r["gold_intent"], r["pred_intent"]) for r in last)
    labels = INTENTS + sorted({r["pred_intent"] for r in last} - set(INTENTS))
    results["confusion_matrix"] = {
        "labels_actual": INTENTS, "labels_predicted": labels,
        "matrix": [[cm.get((a, p), 0) for p in labels] for a in INTENTS]}

    json.dump(results, open(os.path.join(HERE, "eval_results.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "eval_per_item.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(last[0].keys()))
        w.writeheader(); w.writerows(last)
    with open(os.path.join(HERE, "eval_confusion_matrix.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["actual \\ predicted"] + labels)
        for a, row in zip(INTENTS, results["confusion_matrix"]["matrix"]):
            w.writerow([a] + row)

    print("=" * 62)
    print("intent accuracy : %.4f" % results["intent_accuracy"])
    if args.runs > 1:
        print("  per run       : %s  (sd %.4f)"
              % (results["intent_accuracy_per_run"], results["intent_accuracy_stdev"]))
    print("macro F1        : %.4f" % results["macro_f1"])
    print("latency ms      : mean %(mean)s  median %(median)s  p95 %(p95)s  max %(max)s"
          % results["latency_ms"])
    print("by language     : %s" % results["accuracy_by_language"])
    print("slot accuracy   :")
    for k, v in results["slot_accuracy"].items():
        print("   %-38s %s" % (k, v))
    if "rule_fallback" in results:
        print("rule fallback   : %s" % results["rule_fallback"])
    if results["errors"]:
        print("!! %d item(s) errored - check that Ollama stayed up" % results["errors"])
    print("=" * 62)
    print("wrote eval_results.json, eval_per_item.csv, eval_confusion_matrix.csv")


if __name__ == "__main__":
    main()
