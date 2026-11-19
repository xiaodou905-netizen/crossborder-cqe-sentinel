"""Read normalized review exports and generate local demo events."""

import csv
from datetime import date
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from .tagger import Review


REQUIRED_FIELDS = {"review_id", "date", "sku", "stars", "review_text"}
OPTIONAL_FIELDS = {"brand", "supplier", "lot"}
SIMULATED_PACKAGING = (
    (1, "The outer box is sound. The crushed inner tray let the handle hit the mounting plate."),
    (1, "开箱看到内衬压碎，面板在内盒里擦出了两条新划痕。"),
    (2, "The crushed foam insert was flat at one corner and the faceplate came loose in the box."),
    (1, "The collapsed insert slid away from the lock body. The retail carton showed no damage."),
    (2, "外箱没有破损，珍珠棉塌陷后锁体与配件袋直接接触。"),
)


def parse_reviews(source: TextIO) -> list[Review]:
    reader = csv.DictReader(source, strict=True)
    try:
        headers = [name.strip().lower() for name in (reader.fieldnames or [])]
        missing = REQUIRED_FIELDS.difference(headers)
        if missing:
            raise ValueError(f"CSV 缺少必填列：{', '.join(sorted(missing))}。")
        if len(headers) != len(set(headers)) or "" in headers:
            raise ValueError("CSV 表头有重复列名或空列名。")
        reader.fieldnames = headers

        ids = set()
        rows = []
        for values in reader:
            if None in values or None in values.values():
                raise ValueError(f"CSV 第 {reader.line_num} 行的字段数与表头不一致。")
            fields = {name: values[name] for name in REQUIRED_FIELDS}
            fields.update({name: values.get(name, "").strip() or "未标注" for name in OPTIONAL_FIELDS})
            try:
                row = Review.model_validate(fields)
            except ValidationError as exc:
                invalid = ", ".join(sorted(str(error["loc"][0]) for error in exc.errors()))
                raise ValueError(f"CSV 第 {reader.line_num} 行字段无效（{invalid}）。") from None
            if row.review_id in ids:
                raise ValueError(f"CSV 第 {reader.line_num} 行的评论编号重复。")
            ids.add(row.review_id)
            rows.append(row)
    except csv.Error:
        raise ValueError(f"CSV 第 {reader.line_num} 行附近格式无效。请检查引号、列分隔符和正文换行。") from None

    if not rows:
        raise ValueError("CSV 中没有可分析的评分记录。")
    return rows


def load_reviews(path: Path) -> list[Review]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return parse_reviews(source)


def simulated_packaging(as_of: date, existing_count: int) -> list[Review]:
    return [
        Review(
            review_id=f"VOC-SIM-{existing_count + offset:05d}", date=as_of, brand="Brand-Mock",
            sku="SL-Pro-US", supplier="Supplier-A", lot="LOT-PU-04", stars=stars, review_text=text,
        )
        for offset, (stars, text) in enumerate(SIMULATED_PACKAGING, start=1)
    ]
