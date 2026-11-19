"""Retrieve historical 8D evidence with bilingual TF-IDF and keyword overlap."""

import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .alert_engine import Signal
from .tagger import ISSUES, Category, Issue


MIN_COSINE = 0.09
MIN_SIMILARITY = 0.16
CANDIDATE_POOL = 8


@dataclass(frozen=True)
class Case:
    case_id: str
    title: str
    category: Category
    closed_on: date
    keywords: tuple[str, ...]
    root_cause: str
    corrective_action: str
    verification: str
    issue: Issue | None = None


@dataclass(frozen=True)
class Recommendation:
    case: Case
    matched_keywords: tuple[str, ...]
    score: float
    cosine: float
    keyword_coverage: float
    issue_match: bool
    candidate_rank: int


def read_cases(path: Path) -> list[Case]:
    cases = []
    for block in re.split(r"(?m)^## ", path.read_text(encoding="utf-8"))[1:]:
        heading, _, body = block.partition("\n")
        try:
            case_id, title = (part.strip() for part in heading.split("｜", 1))
            metadata = dict(re.findall(r"(?m)^- (类别|失效代码|结案日期|检索词)：([^\n]+)$", body))
            stages = {
                name: content.strip()
                for name, content in re.findall(r"(?ms)^### (D[1-8])\b[^\n]*\n(.*?)(?=^### D[1-8]\b|\Z)", body)
            }
            if stages.keys() != {f"D{i}" for i in range(1, 9)} or not all(stages.values()):
                raise ValueError("all eight 8D stages are required")
            keywords = tuple(term.strip() for term in metadata["检索词"].split("|") if term.strip())
            if not case_id or not title or not keywords:
                raise ValueError("case ID, title and keywords are required")
            category = Category(metadata["类别"].strip())
            issue = Issue(metadata["失效代码"].strip()) if "失效代码" in metadata else None
            if issue and ISSUES[issue].category != category:
                raise ValueError("8D issue code conflicts with its category")
            cases.append(Case(
                case_id, title, category, date.fromisoformat(metadata["结案日期"].strip()),
                keywords, stages["D4"], stages["D5"], stages["D6"], issue,
            ))
        except (ValueError, KeyError):
            raise ValueError(f"8D 案例格式无效：{heading}") from None
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("8D 案例库为空或存在重复案例编号。")
    return cases


def tokenize(text: str) -> list[str]:
    tokens = []
    for run in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.casefold()):
        if run.isascii() or len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


class CaseIndex:
    def __init__(self, cases: list[Case]):
        self.cases = cases
        documents = [
            tokenize(" ".join((case.title, *case.keywords, case.root_cause, case.corrective_action, case.verification)))
            for case in self.cases
        ]
        frequency = Counter(term for doc in documents for term in set(doc))
        self.idf = {term: math.log((len(documents) + 1) / (count + 1)) + 1 for term, count in frequency.items()}
        self.vectors = [self.vectorize(doc) for doc in documents]

    def vectorize(self, terms: list[str]) -> dict[str, float]:
        weights = {
            term: (1 + math.log(count)) * self.idf[term]
            for term, count in Counter(terms).items() if term in self.idf
        }
        length = math.sqrt(sum(weight * weight for weight in weights.values()))
        return {term: weight / length for term, weight in weights.items()} if length else {}

    def search(
        self, category: Category, issue: Issue | None, title: str, keywords: list[str],
        as_of: date, limit: int = 3,
    ) -> list[Recommendation]:
        phrase_tokens = [tokenize(text) for text in (title, *keywords)]
        query = self.vectorize([term for phrase in phrase_tokens for term in phrase])
        query_phrases = [f" {' '.join(phrase)} " for phrase in phrase_tokens]

        candidates = []
        for case, index in zip(self.cases, self.vectors):
            if case.category != category or case.closed_on > as_of:
                continue
            cosine = sum(weight * index.get(term, 0.0) for term, weight in query.items())
            if cosine >= MIN_COSINE:
                candidates.append((case, cosine))
        candidates.sort(key=lambda item: (-item[1], item[0].case_id))

        matches = []
        for rank, (case, cosine) in enumerate(candidates[:CANDIDATE_POOL], start=1):
            found = tuple(
                keyword for keyword in case.keywords
                if (tokens := tokenize(keyword)) and any(f" {' '.join(tokens)} " in phrase for phrase in query_phrases)
            )
            coverage = len(found) / len(case.keywords)
            issue_match = issue is not None and case.issue == issue
            score = 0.65 * cosine + 0.2 * coverage + 0.15 * issue_match
            if score >= MIN_SIMILARITY:
                matches.append(Recommendation(
                    case, found, round(score, 4), round(cosine, 4), round(coverage, 4), issue_match, rank,
                ))
        return sorted(matches, key=lambda item: (-item.score, item.candidate_rank, item.case.case_id))[:limit]

    def recommend(self, signal: Signal, as_of: date) -> Recommendation | None:
        matches = self.search(signal.category, signal.issue, signal.title, signal.keywords, as_of, limit=1)
        return matches[0] if matches else None
