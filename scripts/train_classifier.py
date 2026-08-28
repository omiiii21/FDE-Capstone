#!/usr/bin/env python3
"""Train the intent and urgency classifiers and write the model artefact.

Run:  python -m scripts.train_classifier

The splits here are grouped by normalised ticket body, not random. The
development set contains 500 tickets but only 215 distinct bodies, so a random
split puts the same text on both sides of the line and the accuracy it reports
is meaningless. The difference is not small: 99.2% on a random split against
93% on a body-disjoint one. Everything reported in the evaluation uses the
grouped figure, and the reasoning is in ADR-005.

Temperature is fitted on pooled out-of-fold predictions rather than on the
training data, because a temperature fitted on data the weights already saw
comes out near 1.0 and calibrates nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingest import load_tickets
from src.linear import ConfidenceCalibrator, LogisticRegression, TfidfVectoriser, _softmax

SEED = 20260830  # the day I ran this first; fixed so the artefact is reproducible


def body_key(ticket) -> str:
    return re.sub(r"\s+", " ", ticket.body.strip().lower())


def grouped_folds(tickets, n_folds: int = 5) -> list[tuple[list[int], list[int]]]:
    """Split on distinct bodies so no text appears in both halves."""
    groups: dict[str, list[int]] = defaultdict(list)
    for i, ticket in enumerate(tickets):
        groups[body_key(ticket)].append(i)
    keys = sorted(groups)
    rng = np.random.default_rng(SEED)
    rng.shuffle(keys)
    folds = []
    for f in range(n_folds):
        test_keys = set(keys[f::n_folds])
        test = [i for k in test_keys for i in groups[k]]
        train = [i for k in keys if k not in test_keys for i in groups[k]]
        folds.append((sorted(train), sorted(test)))
    return folds


def per_class_report(y_true, y_pred, classes) -> dict:
    rows = {}
    for c in classes:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == c and p == c)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != c and p == c)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == c and p != c)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        rows[c] = {"precision": precision, "recall": recall, "f1": f1, "support": tp + fn}
    support = sum(r["support"] for r in rows.values())
    rows["__macro__"] = {
        "precision": sum(r["precision"] for c, r in rows.items() if c in classes) / len(classes),
        "recall": sum(r["recall"] for c, r in rows.items() if c in classes) / len(classes),
        "f1": sum(r["f1"] for c, r in rows.items() if c in classes) / len(classes),
        "support": support,
    }
    rows["__weighted_precision__"] = {
        "precision": sum(rows[c]["precision"] * rows[c]["support"] for c in classes) / max(support, 1)
    }
    return rows


def calibration_bins(confidences, correct, edges=(0.0, 0.3, 0.5, 0.65, 0.8, 0.9, 0.95, 1.01)) -> list[dict]:
    out = []
    for lo, hi in zip(edges, edges[1:]):
        group = [(c, k) for c, k in zip(confidences, correct) if lo <= c < hi]
        if not group:
            continue
        stated = sum(c for c, _ in group) / len(group)
        observed = sum(1 for _, k in group if k) / len(group)
        out.append(
            {
                "lower": lo,
                "upper": min(hi, 1.0),
                "n": len(group),
                "stated": round(stated, 4),
                "observed": round(observed, 4),
                "gap_points": round(100 * (stated - observed), 2),
            }
        )
    return out


def expected_calibration_error(bins) -> float:
    total = sum(b["n"] for b in bins)
    return sum(b["n"] * abs(b["stated"] - b["observed"]) for b in bins) / total if total else 0.0


def train_target(tickets, target: str, folds, *, vec_kwargs, lr_kwargs):
    texts = [t.text for t in tickets]
    labels = [t.labels[target] for t in tickets]
    classes = sorted(set(labels))

    oof_logits: dict[int, np.ndarray] = {}
    fold_accuracy = []
    all_true, all_pred = [], []

    for train_idx, test_idx in folds:
        vec = TfidfVectoriser(**vec_kwargs)
        X_train = vec.fit_transform([texts[i] for i in train_idx])
        model = LogisticRegression(**lr_kwargs).fit(X_train, [labels[i] for i in train_idx])
        X_test = vec.transform([texts[i] for i in test_idx])
        logits = model.decision(X_test)
        # Fold models can miss a rare class entirely; map onto the global
        # class list so the out-of-fold matrix is consistent.
        aligned = np.full((len(test_idx), len(classes)), -30.0)
        for j, c in enumerate(model.classes):
            aligned[:, classes.index(c)] = logits[:, j]
        for row, i in enumerate(test_idx):
            oof_logits[i] = aligned[row]
        preds = [classes[int(np.argmax(aligned[row]))] for row in range(len(test_idx))]
        truth = [labels[i] for i in test_idx]
        fold_accuracy.append(sum(1 for p, t in zip(preds, truth) if p == t) / len(truth))
        all_true.extend(truth)
        all_pred.extend(preds)

    # The shipped model. Weights are fitted on the first four folds' worth of
    # bodies and the temperature on the fifth, using that same model's logits.
    #
    # Fitting the temperature on the pooled out-of-fold logits instead would use
    # all 500 tickets, and it is what I did first. It does not work: the
    # fold models are trained on 80% of the data and produce flatter logits than
    # the full model, so the temperature that calibrates them (0.40) sharpens
    # the full model until every prediction reads 1.000. Calibration has to be
    # fitted against the logits of the model that will actually be shipped, even
    # though that costs a fifth of the training data.
    fit_idx, calib_idx = folds[0][0], folds[0][1]
    vec = TfidfVectoriser(**vec_kwargs)
    X_fit = vec.fit_transform([texts[i] for i in fit_idx])
    model = LogisticRegression(**lr_kwargs).fit(X_fit, [labels[i] for i in fit_idx])

    X_calib = vec.transform([texts[i] for i in calib_idx])
    calib_labels = [labels[i] for i in calib_idx]
    calib_logits = model.decision(X_calib)
    aligned = np.full((len(calib_idx), len(classes)), -30.0)
    for j, c in enumerate(model.classes):
        aligned[:, classes.index(c)] = calib_logits[:, j]

    # Temperature scaling, kept for the comparison reported in ADR-003. It
    # reduces expected calibration error and it is still not what gets shipped,
    # because the only way it can calibrate this distribution is to sharpen it
    # until the routing threshold has nothing left to separate.
    holder = LogisticRegression()
    holder.classes = classes
    temperature = holder.fit_temperature(aligned, calib_labels)

    correct = [classes[int(np.argmax(row))] == calib_labels[i] for i, row in enumerate(aligned)]
    raw = _softmax(aligned)
    ordered_raw = np.sort(raw, axis=1)[:, ::-1]
    max_prob = ordered_raw[:, 0]
    margin = ordered_raw[:, 0] - ordered_raw[:, 1]

    temp_bins = calibration_bins(_softmax(aligned / temperature).max(axis=1).tolist(), correct)
    uncal_bins = calibration_bins(max_prob.tolist(), correct)

    # What actually ships: P(correct) from the top probability and the margin.
    calibrator = ConfidenceCalibrator().fit(max_prob, margin, correct)
    model.temperature = 1.0
    conf = [calibrator.predict(float(m), float(g)) for m, g in zip(max_prob, margin)]
    bins = calibration_bins(conf, correct)

    report = {
        "target": target,
        "confidence_spread": {
            "uncalibrated_p95_share_above_0_99": round(float((max_prob > 0.99).mean()), 4),
            "calibrated_share_above_0_99": round(float(np.mean([1.0 if c > 0.99 else 0.0 for c in conf])), 4),
            "calibrated_min": round(min(conf), 4),
            "calibrated_max": round(max(conf), 4),
        },
        "classes": classes,
        "n_tickets": len(tickets),
        "n_distinct_bodies": len({body_key(t) for t in tickets}),
        "n_fitted_on": len(fit_idx),
        "n_calibrated_on": len(calib_idx),
        "fold_accuracy": [round(a, 4) for a in fold_accuracy],
        "mean_accuracy_body_disjoint": round(float(np.mean(fold_accuracy)), 4),
        "std_accuracy": round(float(np.std(fold_accuracy)), 4),
        "per_class": per_class_report(all_true, all_pred, classes),
        "temperature_if_used": temperature,
        "calibrator": calibrator.to_dict(),
        "calibration_shipped": bins,
        "calibration_temperature_scaled": temp_bins,
        "calibration_uncalibrated": uncal_bins,
        "ece_shipped": round(expected_calibration_error(bins), 4),
        "ece_temperature_scaled": round(expected_calibration_error(temp_bins), 4),
        "ece_uncalibrated": round(expected_calibration_error(uncal_bins), 4),
    }
    return vec, model, calibrator, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/development_tickets.json")
    parser.add_argument("--output", default="models/intent_classifier.json")
    parser.add_argument("--report", default="evaluation/results/classifier_training_report.json")
    args = parser.parse_args()

    tickets = [t for t in load_tickets(args.input) if t.labels.get("intent")]
    print(f"{len(tickets)} labelled tickets, {len({body_key(t) for t in tickets})} distinct bodies")
    folds = grouped_folds(tickets)

    intent_vec, intent_model, intent_calibrator, intent_report = train_target(
        tickets,
        "intent",
        folds,
        vec_kwargs={"min_df": 1, "max_features": 20000, "ngram": 2},
        lr_kwargs={"l2": 3e-5, "learning_rate": 1.2, "epochs": 1200},
    )
    urgency_vec, urgency_model, urgency_calibrator, urgency_report = train_target(
        tickets,
        "urgency",
        folds,
        vec_kwargs={"min_df": 1, "max_features": 20000, "ngram": 2},
        lr_kwargs={"l2": 2e-4, "learning_rate": 1.0, "epochs": 900},
    )

    for report in (intent_report, urgency_report):
        print(
            f"\n{report['target']}: body-disjoint accuracy "
            f"{report['mean_accuracy_body_disjoint']:.1%} (+/- {report['std_accuracy']:.1%}), "
            f"macro precision {report['per_class']['__macro__']['precision']:.1%}, "
            f"weighted precision {report['per_class']['__weighted_precision__']['precision']:.1%}"
        )
        print(
            f"  ECE uncalibrated {report['ece_uncalibrated']:.3f} | "
            f"temperature-scaled {report['ece_temperature_scaled']:.3f} | "
            f"shipped {report['ece_shipped']:.3f}"
        )
        spread = report["confidence_spread"]
        print(
            f"  share of predictions above 0.99: "
            f"{spread['uncalibrated_p95_share_above_0_99']:.0%} raw -> "
            f"{spread['calibrated_share_above_0_99']:.0%} shipped "
            f"(range {spread['calibrated_min']:.2f}-{spread['calibrated_max']:.2f})"
        )

    artefact = {
        "format_version": 2,
        "trained_from": args.input,
        "seed": SEED,
        "intent": {
            "vectoriser": intent_vec.to_dict(),
            "model": intent_model.to_dict(),
            "calibrator": intent_calibrator.to_dict(),
        },
        "urgency": {
            "vectoriser": urgency_vec.to_dict(),
            "model": urgency_model.to_dict(),
            "calibrator": urgency_calibrator.to_dict(),
        },
        "metrics": {
            "intent_accuracy_body_disjoint": intent_report["mean_accuracy_body_disjoint"],
            "urgency_accuracy_body_disjoint": urgency_report["mean_accuracy_body_disjoint"],
            "intent_ece_shipped": intent_report["ece_shipped"],
            "urgency_ece_shipped": urgency_report["ece_shipped"],
        },
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artefact), encoding="utf-8")
    print(f"\nmodel written to {out} ({out.stat().st_size / 1024:.0f} KB)")

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps({"intent": intent_report, "urgency": urgency_report}, indent=2), encoding="utf-8"
    )
    print(f"report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
