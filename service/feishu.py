"""Build a local Feishu card preview from the selected report."""


def plain_text(content: str) -> dict:
    return {"tag": "plain_text", "content": content}


def feishu_card(report: dict) -> dict:
    alerts = report["alerts"]
    elements = []
    for alert in alerts:
        recent, baseline = alert["recent"], alert["baseline"]
        recommendation = alert["recommendation"]
        case = recommendation["case"] if recommendation else None
        lots = "、".join(f"{lot}（{count} 条）" for lot, count in alert["lots"].items())
        lines = [
            f"{alert['level']}｜{alert['title']}",
            f"{alert['sku']} / {alert['supplier']}  ·  批次 {lots}",
            f"近 7 天 {recent['count']}/{recent['sample']} 条（{recent['share']:.1f}%），"
            f"基线 {baseline['count']}/{baseline['sample']} 条（{baseline['share']:.1f}%）",
            f"可参考 {case['case_id']} {case['title']}" if case else "请回查批次、原始评论与检验记录。",
        ]
        inspections = alert.get("quality_evidence", {}).get("inspections", [])
        if inspections:
            lines.append("现场记录 " + "、".join(item["inspection_id"] for item in inspections[:3]))
        elements.append({"tag": "div", "text": plain_text("\n".join(lines))})
    if not elements:
        elements.append({"tag": "div", "text": plain_text("当前窗口无达到阈值的质量预警。")})

    source_label = {"demo": "示例数据", "import": "导入数据", "sync": "同步快照"}[report["data_source"]]
    return {
        "simulated": True,
        "payload": {
            "msg_type": "interactive",
            "card": {
                "schema": "2.0",
                "header": {
                    "template": "red" if alerts else "blue",
                    "title": plain_text("苗头性客诉预警"),
                    "subtitle": plain_text(f"{report['as_of']} · {len(alerts)} 条预警 · {source_label}"),
                },
                "body": {"direction": "vertical", "elements": elements},
            },
        },
    }
