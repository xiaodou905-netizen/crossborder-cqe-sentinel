"""Evaluate the shipped rules, retriever, and historical alert replay offline."""

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

from core.alert_engine import evaluate_signals
from core.reporting import Analyzer
from core.tagger import Category, Issue, classify_demo


HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "docs" / "evaluation.md"
LABELS = [*(category.value for category in Category), None]


def read_jsonl(name: str) -> list[dict]:
    return [json.loads(line) for line in (HERE / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def score_voc() -> dict:
    rows = read_jsonl("voc_gold.jsonl")
    pairs = [(row, classify_demo(row["text"])) for row in rows]
    metrics = []
    for label in LABELS:
        tp = sum(gold["category"] == tag.category == label for gold, tag in pairs)
        predicted = sum(tag.category == label for _, tag in pairs)
        support = sum(gold["category"] == label for gold, _ in pairs)
        metrics.append({
            "label": label or "待复核", "support": support,
            "precision": tp / predicted if predicted else 0,
            "recall": tp / support if support else 0,
            "f1": 2 * tp / (predicted + support) if predicted + support else 0,
        })
    modes = [(gold, tag) for gold, tag in pairs if gold["issue"] is not None]
    correct_modes = sum(gold["issue"] == tag.issue for gold, tag in modes)
    abstain = [(gold, tag) for gold, tag in pairs if gold["category"] is None]
    errors = [
        {"id": gold["id"], "expected": gold["category"], "predicted": tag.category.value if tag.category else None}
        for gold, tag in pairs if gold["category"] != tag.category
    ]
    return {
        "n": len(rows), "macro_f1": mean(item["f1"] for item in metrics),
        "modes_correct": correct_modes, "modes_total": len(modes),
        "abstain_correct": sum(tag.category is None for _, tag in abstain), "abstain_total": len(abstain),
        "categories": metrics, "errors": errors,
    }


def score_retrieval(analyzer: Analyzer) -> dict:
    rows = read_jsonl("retrieval_gold.jsonl")
    positive_ranks, abstentions, errors = [], [], []
    for row in rows:
        matches = analyzer.cases.search(
            Category(row["category"]), Issue(row["issue"]) if row["issue"] else None,
            row["title"], row["keywords"], date.fromisoformat(row["as_of"]),
        )
        ids = [item.case.case_id for item in matches]
        predicted = ids[0] if ids else None
        expected = row["expected"]
        if expected is None:
            abstentions.append(not ids)
        else:
            positive_ranks.append(ids.index(expected) + 1 if expected in ids else 0)
        if predicted != expected:
            errors.append({"id": row["id"], "expected": expected, "predicted": predicted})
    return {
        "n": len(rows), "positive": len(positive_ranks), "recall_at_3": mean(rank > 0 for rank in positive_ranks),
        "mrr_at_3": mean(1 / rank if rank else 0 for rank in positive_ranks),
        "abstain_correct": sum(abstentions), "abstain_total": len(abstentions), "errors": errors,
    }


def score_replay(analyzer: Analyzer) -> dict:
    planted = {
        ("SL-Pro-US", Issue.TRAY): date(2026, 11, 15),
        ("SL-Max-EU", Issue.COLD_FINGERPRINT): date(2026, 11, 16),
    }
    sample = analyzer.prepare_demo(None)
    first_seen = {}
    extra = []
    days = []
    for shift in range(7, -1, -1):
        end = sample.as_of - timedelta(days=shift)
        signals = evaluate_signals(sample.tagged, end)
        days.append({"date": end.isoformat(), "alerts": len(signals)})
        for signal in signals:
            key = (signal.sku, signal.issue)
            onset = planted.get(key)
            if onset is None or end < onset:
                extra.append({"date": end.isoformat(), "sku": signal.sku, "issue": signal.issue})
            else:
                first_seen.setdefault(key, end)
    return {
        "days": days, "extra_alert_days": len(extra), "extra_alerts": extra, "detected": len(first_seen),
        "planted": [
            {"sku": sku, "issue": issue.value, "onset": onset.isoformat(),
             "first_alert": first_seen[(sku, issue)].isoformat() if (sku, issue) in first_seen else None,
             "delay_days": (first_seen[(sku, issue)] - onset).days if (sku, issue) in first_seen else None}
            for (sku, issue), onset in planted.items()
        ],
    }


def evaluate() -> dict:
    analyzer = Analyzer(demo_mode=True)
    return {"voc": score_voc(), "retrieval": score_retrieval(analyzer), "replay": score_replay(analyzer)}


def markdown(result: dict) -> str:
    voc, rag, replay = result["voc"], result["retrieval"], result["replay"]
    lines = [
        "# 可复跑评测", "",
        "以下为仓库内人工标注的合成挑战集和历史样例回放。评测使用无密钥规则与本地 8D 检索。", "",
        "| 检验项 | 样本与分母 | 结果 |", "| --- | --- | --- |",
        f"| VOC 五类 Macro-F1（含待复核） | {voc['n']} 条 | **{voc['macro_f1']:.1%}** |",
        f"| 已知失效代码准确匹配 | {voc['modes_total']} 条 | {voc['modes_correct']}/{voc['modes_total']} |",
        f"| 待复核样本正确保留 | {voc['abstain_total']} 条 | {voc['abstain_correct']}/{voc['abstain_total']} |",
        f"| 8D Recall@3 / MRR@3 | {rag['positive']} 条有关联案例 | **{rag['recall_at_3']:.3f} / {rag['mrr_at_3']:.3f}** |",
        f"| 8D 正确弃答 | {rag['abstain_total']} 条 | {rag['abstain_correct']}/{rag['abstain_total']} |",
        f"| 预警回放 | {len(replay['days'])} 个回放日 | {replay['detected']}/{len(replay['planted'])} 处预埋信号，{replay['extra_alert_days']} 条额外告警 |",
        "", "## VOC 分类", "", "| 类别 | 标注数 | Precision | Recall | F1 |", "| --- | ---: | ---: | ---: | ---: |",
    ]
    lines += [
        f"| {item['label']} | {item['support']} | {item['precision']:.3f} | {item['recall']:.3f} | {item['f1']:.3f} |"
        for item in voc["categories"]
    ]
    lines += ["", "## 逐日回放", "", "| SKU | 首条植入 | 首次预警 | 延迟天数 |", "| --- | --- | --- | ---: |"]
    lines += [
        f"| {item['sku']} | {item['onset']} | {item['first_alert'] or '—'} | {item['delay_days']} |"
        for item in replay["planted"]
    ]
    if replay["extra_alerts"]:
        lines += ["", "超出预埋标注的信号。"]
        lines += [
            f"- {item['date']} · `{item['sku']}` · `{item['issue'].value if item['issue'] else '未标准化'}`"
            for item in replay["extra_alerts"]
        ]
    lines += ["", "## 错例编号", "", "| 用例 | 期望 | 实际 |", "| --- | --- | --- |"]
    lines += [
        f"| {item['id']} | {item['expected'] or '待复核 / 弃答'} | {item['predicted'] or '待复核 / 弃答'} |"
        for item in voc["errors"] + rag["errors"]
    ]
    lines += [
        "", "编号可回查 [VOC 标签集](../benchmarks/voc_gold.jsonl) 和 [检索集](../benchmarks/retrieval_gold.jsonl)。",
        "逐日回放反映这组固定样本的触发时点。真实跨站点效果需另做人工盲标和带销量分母的历史回放。",
        "", "用 `python -m benchmarks.evaluate --format markdown` 重算。", "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--check", action="store_true", help="Check the versioned evaluation report")
    args = parser.parse_args()
    result = evaluate()
    if args.check:
        if RESULTS.read_text(encoding="utf-8") != markdown(result):
            parser.exit(1, "docs/evaluation.md is out of date. Regenerate it with --format markdown.\n")
        print("Evaluation matches docs/evaluation.md")
    elif args.format == "markdown":
        print(markdown(result), end="")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
