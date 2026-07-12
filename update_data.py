#!/usr/bin/env python3
"""Hang on! 신용잔고 페이지에서 검증된 신용융자·고객예탁금 시계열을 수집한다.

핵심 원칙
1. 페이지에 실제로 표시된 최신 날짜/비율/금액을 먼저 읽는다.
2. 네트워크 응답의 여러 후보 시계열 중 표시값과 일치하는 후보만 선택한다.
3. 최신 표시값과 맞지 않으면 잘못된 데이터를 게시하지 않고 실패 처리한다.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from playwright.async_api import Response, async_playwright

SOURCE_URL = "https://www.hangon.co.kr/credit-balance"
ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data.json"
DEBUG = ROOT / "debug_capture.json"

DATE_KEYS = (
    "date", "day", "dt", "base_date", "baseDate", "basDt", "trdDd", "tradeDate",
    "trade_date", "bizDate", "businessDate", "stck_bsop_date", "일자", "날짜", "기준일", "기준일자",
)
CREDIT_PATTERNS = (
    "credit", "creditbalance", "creditloan", "credit_loan", "creditamount", "crdt", "crd",
    "loanbalance", "marginloan", "융자", "융자잔고", "신용잔고", "신용융자", "신용거래융자",
)
DEPOSIT_PATTERNS = (
    "deposit", "customerdeposit", "investordeposit", "customer_deposit", "investor_deposit",
    "custdps", "dps", "예탁", "예탁금", "고객예탁금", "투자자예탁금", "예수금",
)
IGNORE_PATTERNS = (
    "ratio", "rate", "percent", "percentage", "비율", "증감", "change", "changeamount", "change_rate",
)
SCALES_TO_TRILLION = tuple(10.0 ** (-power) for power in range(0, 13))


def norm_key(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", str(value).lower())


def key_matches(key: str, patterns: Iterable[str]) -> bool:
    normalized = norm_key(key)
    return any(norm_key(pattern) in normalized for pattern in patterns)


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None

    original = str(value).strip()
    text = original.replace(",", "").replace(" ", "")
    if not text or text in {"-", "—", "null", "None", "nan"}:
        return None

    sign = -1 if text.startswith("-") else 1
    unsigned = text.lstrip("+-")
    total = 0.0
    matched = False
    for unit, multiplier in (("조", 1e12), ("억", 1e8), ("만", 1e4)):
        match = re.search(rf"([0-9]+(?:\.[0-9]+)?){unit}", unsigned)
        if match:
            total += float(match.group(1)) * multiplier
            matched = True
    if matched:
        return sign * total

    match = re.search(r"-?[0-9]+(?:\.[0-9]+)?", original.replace(",", ""))
    return float(match.group()) if match else None


def parse_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    candidates = [text]
    if len(digits) == 8:
        candidates.insert(0, f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}")
    elif len(digits) == 6:
        candidates.insert(0, f"20{digits[:2]}-{digits[2:4]}-{digits[4:6]}")

    for candidate in candidates:
        candidate = candidate.replace(".", "-").replace("/", "-")
        candidate = re.sub(r"\s.*$", "", candidate)
        for fmt in ("%Y-%m-%d", "%y-%m-%d"):
            try:
                parsed = datetime.strptime(candidate, fmt)
                if 2000 <= parsed.year <= 2100:
                    return parsed.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def date_distance_days(left: str, right: str) -> int:
    return abs((date.fromisoformat(left) - date.fromisoformat(right)).days)


def first_reasonable(values: Iterable[float | None], low: float, high: float) -> float | None:
    for value in values:
        if value is not None and low <= value <= high:
            return value
    return None


@dataclass(frozen=True)
class Snapshot:
    date: str | None = None
    ratio: float | None = None
    credit_trillion: float | None = None
    deposit_trillion: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "ratio": self.ratio,
            "credit_trillion": self.credit_trillion,
            "deposit_trillion": self.deposit_trillion,
        }


def extract_snapshot(*texts: str) -> Snapshot:
    joined = "\n".join(text for text in texts if text)
    compact = re.sub(r"[\t\r]+", " ", joined)

    ratio_matches: list[float | None] = []
    for pattern in (
        r"예탁금\s*대비\s*신용(?:\s*비율)?[^0-9]{0,60}([0-9]+(?:\.[0-9]+)?)\s*%",
        r"신용\s*비율[^0-9]{0,40}([0-9]+(?:\.[0-9]+)?)\s*%",
        r"ratio[^0-9]{0,15}([0-9]+(?:\.[0-9]+)?)",
    ):
        ratio_matches.extend(parse_number(match) for match in re.findall(pattern, compact, flags=re.I | re.S))
    ratio = first_reasonable(ratio_matches, 5.0, 80.0)

    date_value = None
    for pattern in (
        r"기준\s*(20\d{2}[-./]\d{1,2}[-./]\d{1,2})",
        r"(20\d{2}[-./]\d{1,2}[-./]\d{1,2})\s*기준",
    ):
        match = re.search(pattern, compact)
        if match and (parsed := parse_date(match.group(1))):
            date_value = parsed
            break

    def labelled_trillion(label_patterns: tuple[str, ...], low: float, high: float) -> float | None:
        results: list[float | None] = []
        labels = "|".join(label_patterns)
        for match in re.finditer(
            rf"(?:{labels})[\s\S]{{0,90}}?([0-9]+(?:\.[0-9]+)?)\s*조(?:원)?",
            compact,
            flags=re.I,
        ):
            results.append(parse_number(match.group(1)))
        return first_reasonable(results, low, high)

    credit = labelled_trillion(
        (r"신용융자\s*잔고", r"신용\s*잔고", r"신용거래융자", r"Credit"), 1.0, 100.0
    )
    deposit = labelled_trillion(
        (r"고객예탁금", r"투자자예탁금", r"Deposit"), 10.0, 300.0
    )

    if credit and deposit:
        calculated = credit / deposit * 100
        if ratio is None or abs(calculated - ratio) <= 1.0:
            ratio = calculated

    return Snapshot(
        date=date_value,
        ratio=round(ratio, 4) if ratio is not None else None,
        credit_trillion=round(credit, 4) if credit is not None else None,
        deposit_trillion=round(deposit, 4) if deposit is not None else None,
    )


def iter_arrays(node: Any, path: str = "root"):
    if isinstance(node, list):
        if node and all(isinstance(item, dict) for item in node):
            yield path, node
        for index, item in enumerate(node):
            yield from iter_arrays(item, f"{path}[{index}]")
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from iter_arrays(value, f"{path}.{key}")


def find_key(row: dict[str, Any], patterns: Iterable[str], *, exclude: Iterable[str] = ()) -> str | None:
    scored: list[tuple[int, str]] = []
    for key in row:
        normalized = norm_key(key)
        if any(norm_key(item) in normalized for item in exclude):
            continue
        score = 0
        for pattern in patterns:
            normalized_pattern = norm_key(pattern)
            if normalized == normalized_pattern:
                score = max(score, 20)
            elif normalized.startswith(normalized_pattern) or normalized.endswith(normalized_pattern):
                score = max(score, 12)
            elif normalized_pattern in normalized:
                score = max(score, 7)
        if score:
            scored.append((score, key))
    return max(scored, default=(0, None))[1]


def primitive_list(node: Any) -> list[Any] | None:
    if isinstance(node, list) and node and all(not isinstance(item, (dict, list)) for item in node):
        return node
    return None


def extract_parallel_arrays(node: Any, path: str = "root") -> list[tuple[str, list[dict[str, Any]]]]:
    found: list[tuple[str, list[dict[str, Any]]]] = []
    if isinstance(node, dict):
        primitive = {key: primitive_list(value) for key, value in node.items()}
        primitive = {key: value for key, value in primitive.items() if value is not None}

        date_key = next(
            (key for key in primitive if key_matches(key, DATE_KEYS) or norm_key(key) in {"labels", "categories", "xaxis"}),
            None,
        )
        credit_key = next(
            (key for key in primitive if key_matches(key, CREDIT_PATTERNS) and not key_matches(key, IGNORE_PATTERNS + DEPOSIT_PATTERNS)),
            None,
        )
        deposit_key = next(
            (key for key in primitive if key_matches(key, DEPOSIT_PATTERNS) and not key_matches(key, IGNORE_PATTERNS)),
            None,
        )
        if date_key and credit_key and deposit_key:
            dates, credits, deposits = primitive[date_key], primitive[credit_key], primitive[deposit_key]
            size = min(len(dates), len(credits), len(deposits))
            if size >= 2:
                found.append((
                    f"{path}:parallel",
                    [{"date": dates[i], "credit": credits[i], "deposit": deposits[i]} for i in range(size)],
                ))

        labels = primitive.get("labels") or primitive.get("categories")
        datasets = node.get("datasets") or node.get("series")
        if labels and isinstance(datasets, list):
            credit_values = None
            deposit_values = None
            for item in datasets:
                if not isinstance(item, dict):
                    continue
                label = item.get("label") or item.get("name") or item.get("title") or ""
                values = primitive_list(item.get("data")) or primitive_list(item.get("values"))
                if not values:
                    continue
                if key_matches(label, CREDIT_PATTERNS) and not key_matches(label, DEPOSIT_PATTERNS + IGNORE_PATTERNS):
                    credit_values = values
                if key_matches(label, DEPOSIT_PATTERNS) and not key_matches(label, IGNORE_PATTERNS):
                    deposit_values = values
            if credit_values and deposit_values:
                size = min(len(labels), len(credit_values), len(deposit_values))
                if size >= 2:
                    found.append((
                        f"{path}:chart-series",
                        [{"date": labels[i], "credit": credit_values[i], "deposit": deposit_values[i]} for i in range(size)],
                    ))

        for key, value in node.items():
            found.extend(extract_parallel_arrays(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(extract_parallel_arrays(value, f"{path}[{index}]"))
    return found


@dataclass
class RawCandidate:
    path: str
    rows: list[dict[str, Any]]
    date_key: str
    credit_key: str
    deposit_key: str


@dataclass
class ScoredCandidate:
    path: str
    series: list[dict[str, float | str]]
    score: float
    details: dict[str, Any]


def candidate_from_array(path: str, rows: list[dict[str, Any]]) -> RawCandidate | None:
    if len(rows) < 2:
        return None
    keys = {key for row in rows[:50] for key in row}
    representative = {key: None for key in keys}
    date_key = find_key(representative, DATE_KEYS)
    credit_key = find_key(representative, CREDIT_PATTERNS, exclude=IGNORE_PATTERNS + DEPOSIT_PATTERNS)
    deposit_key = find_key(representative, DEPOSIT_PATTERNS, exclude=IGNORE_PATTERNS)
    if not (date_key and credit_key and deposit_key) or credit_key == deposit_key:
        return None

    valid = sum(
        1
        for row in rows
        if parse_date(row.get(date_key))
        and parse_number(row.get(credit_key)) is not None
        and parse_number(row.get(deposit_key)) is not None
    )
    if valid < 2:
        return None
    return RawCandidate(path, rows, date_key, credit_key, deposit_key)


def choose_scale(raw_values: list[float], target: float | None, kind: str) -> float:
    finite = sorted(abs(value) for value in raw_values if value and math.isfinite(value))
    if not finite:
        return 1.0
    median = finite[len(finite) // 2]
    target_value = target or (30.0 if kind == "credit" else 100.0)
    plausible = (0.5, 150.0) if kind == "credit" else (5.0, 500.0)

    ranked: list[tuple[float, float]] = []
    for scale in SCALES_TO_TRILLION:
        scaled = median * scale
        penalty = abs(math.log10(max(scaled, 1e-12) / target_value))
        if not plausible[0] <= scaled <= plausible[1]:
            penalty += 10.0
        ranked.append((penalty, scale))
    return min(ranked)[1]


def normalize_candidate(candidate: RawCandidate, snapshot: Snapshot) -> list[dict[str, float | str]]:
    parsed: list[tuple[str, float, float]] = []
    for row in candidate.rows:
        date_value = parse_date(row.get(candidate.date_key))
        credit = parse_number(row.get(candidate.credit_key))
        deposit = parse_number(row.get(candidate.deposit_key))
        if date_value and credit is not None and deposit is not None and credit > 0 and deposit > 0:
            parsed.append((date_value, credit, deposit))
    if len(parsed) < 2:
        return []

    target_row = None
    if snapshot.date:
        target_row = next((row for row in parsed if row[0] == snapshot.date), None)
    reference_rows = [target_row] if target_row else [parsed[-1]]

    credit_scale = choose_scale(
        [row[1] for row in reference_rows if row], snapshot.credit_trillion, "credit"
    )
    deposit_scale = choose_scale(
        [row[2] for row in reference_rows if row], snapshot.deposit_trillion, "deposit"
    )

    by_date: dict[str, dict[str, float | str]] = {}
    for date_value, raw_credit, raw_deposit in parsed:
        credit = raw_credit * credit_scale
        deposit = raw_deposit * deposit_scale
        ratio = credit / deposit * 100
        if not (0.5 <= credit <= 150.0 and 5.0 <= deposit <= 500.0 and 0.5 <= ratio <= 100.0):
            continue
        by_date[date_value] = {
            "date": date_value,
            "credit_trillion": round(credit, 4),
            "deposit_trillion": round(deposit, 4),
            "ratio": round(ratio, 4),
        }
    return [by_date[key] for key in sorted(by_date)]


def score_series(path: str, series: list[dict[str, float | str]], snapshot: Snapshot) -> ScoredCandidate | None:
    if len(series) < 2:
        return None
    latest = series[-1]
    latest_date = str(latest["date"])
    score = min(len(series), 500) * 2.0
    score += min((date.fromisoformat(latest_date) - date.fromisoformat(str(series[0]["date"]))).days, 1000) * 0.1

    normalized_path = norm_key(path)
    for keyword in ("credit", "balance", "deposit", "kofia", "finance", "stock", "신용", "예탁"):
        if norm_key(keyword) in normalized_path:
            score += 100.0

    details: dict[str, Any] = {
        "rows": len(series),
        "first_date": series[0]["date"],
        "latest_date": latest_date,
        "latest_credit": latest["credit_trillion"],
        "latest_deposit": latest["deposit_trillion"],
        "latest_ratio": latest["ratio"],
    }

    if snapshot.date:
        distance = date_distance_days(latest_date, snapshot.date)
        details["date_distance_days"] = distance
        if distance == 0:
            score += 20_000
        elif distance <= 3:
            score += 10_000 - distance * 500
        elif distance <= 7:
            score += 4_000 - distance * 250
        else:
            score -= min(distance, 365) * 400

    if snapshot.ratio is not None:
        difference = abs(float(latest["ratio"]) - snapshot.ratio)
        details["ratio_difference_pp"] = round(difference, 4)
        score += max(0.0, 8_000 - difference * 4_000)
        if difference > 2.0:
            score -= 10_000

    for field, target, weight in (
        ("credit_trillion", snapshot.credit_trillion, 5_000.0),
        ("deposit_trillion", snapshot.deposit_trillion, 5_000.0),
    ):
        if target is None:
            continue
        actual = float(latest[field])
        relative_error = abs(actual - target) / target
        details[f"{field}_relative_error"] = round(relative_error, 5)
        score += max(0.0, weight - relative_error * 50_000)
        if relative_error > 0.15:
            score -= 8_000

    return ScoredCandidate(path, series, score, details)


def build_separate_candidates(payloads: list[dict[str, Any]], snapshot: Snapshot) -> list[ScoredCandidate]:
    credit_series: list[tuple[str, dict[str, float]]] = []
    deposit_series: list[tuple[str, dict[str, float]]] = []

    for payload in payloads:
        for path, rows in iter_arrays(payload["data"], payload["url"]):
            if len(rows) < 2:
                continue
            keys = {key for row in rows[:50] for key in row}
            representative = {key: None for key in keys}
            date_key = find_key(representative, DATE_KEYS)
            if not date_key:
                continue
            credit_key = find_key(representative, CREDIT_PATTERNS, exclude=IGNORE_PATTERNS + DEPOSIT_PATTERNS)
            deposit_key = find_key(representative, DEPOSIT_PATTERNS, exclude=IGNORE_PATTERNS)

            if credit_key:
                values = {
                    parsed_date: parsed_value
                    for row in rows
                    if (parsed_date := parse_date(row.get(date_key)))
                    and (parsed_value := parse_number(row.get(credit_key))) is not None
                    and parsed_value > 0
                }
                if len(values) >= 2:
                    credit_series.append((f"{path}:{credit_key}", values))
            if deposit_key:
                values = {
                    parsed_date: parsed_value
                    for row in rows
                    if (parsed_date := parse_date(row.get(date_key)))
                    and (parsed_value := parse_number(row.get(deposit_key))) is not None
                    and parsed_value > 0
                }
                if len(values) >= 2:
                    deposit_series.append((f"{path}:{deposit_key}", values))

    candidates: list[ScoredCandidate] = []
    for credit_path, credits in credit_series:
        for deposit_path, deposits in deposit_series:
            dates = sorted(set(credits) & set(deposits))
            if len(dates) < 5:
                continue
            rows = [{"date": item, "credit": credits[item], "deposit": deposits[item]} for item in dates]
            raw = RawCandidate(
                path=f"merged:{credit_path}|{deposit_path}",
                rows=rows,
                date_key="date",
                credit_key="credit",
                deposit_key="deposit",
            )
            normalized = normalize_candidate(raw, snapshot)
            if scored := score_series(raw.path, normalized, snapshot):
                candidates.append(scored)
    return candidates


def validate_selected(selected: ScoredCandidate, snapshot: Snapshot) -> None:
    latest = selected.series[-1]
    problems: list[str] = []

    if snapshot.date:
        distance = date_distance_days(str(latest["date"]), snapshot.date)
        if distance > 3:
            problems.append(f"최신 날짜 불일치: 화면 {snapshot.date}, 후보 {latest['date']}")
    else:
        age = (date.today() - date.fromisoformat(str(latest["date"]))).days
        if age > 14:
            problems.append(f"최신 데이터가 {age}일 이상 오래되었습니다.")

    if snapshot.ratio is not None and abs(float(latest["ratio"]) - snapshot.ratio) > 0.6:
        problems.append(f"신용비율 불일치: 화면 {snapshot.ratio:.2f}%, 후보 {float(latest['ratio']):.2f}%")

    for field, target, label in (
        ("credit_trillion", snapshot.credit_trillion, "신용융자 잔고"),
        ("deposit_trillion", snapshot.deposit_trillion, "고객예탁금"),
    ):
        if target is None:
            continue
        actual = float(latest[field])
        tolerance = max(0.5, target * 0.03)
        if abs(actual - target) > tolerance:
            problems.append(f"{label} 불일치: 화면 {target:.2f}조, 후보 {actual:.2f}조")

    recomputed = float(latest["credit_trillion"]) / float(latest["deposit_trillion"]) * 100
    if abs(recomputed - float(latest["ratio"])) > 0.02:
        problems.append("최신 행의 비율 계산이 금액과 일치하지 않습니다.")

    if problems:
        raise RuntimeError("검증 실패 — " + " / ".join(problems))


async def main() -> None:
    payloads: list[dict[str, Any]] = []
    response_log: list[dict[str, Any]] = []
    text_payloads: list[str] = []
    tasks: set[asyncio.Task[Any]] = set()
    body_text = ""
    html = ""
    meta_text = ""

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1440, "height": 1400},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        async def capture(response: Response) -> None:
            entry: dict[str, Any] = {
                "url": response.url,
                "status": response.status,
                "resource_type": response.request.resource_type,
                "content_type": (response.headers.get("content-type") or "").lower(),
            }
            try:
                should_read = (
                    response.request.resource_type in {"xhr", "fetch"}
                    or "json" in entry["content_type"]
                    or "text/x-component" in entry["content_type"]
                )
                if should_read and response.status < 400:
                    body = await response.text()
                    entry["size"] = len(body)
                    if any(term in body.lower() for term in ("credit", "deposit", "신용", "예탁")):
                        text_payloads.append(body[:3_000_000])
                    try:
                        payloads.append({"url": response.url, "data": json.loads(body)})
                        entry["json"] = True
                    except json.JSONDecodeError:
                        entry["json"] = False
                response_log.append(entry)
            except Exception as exc:
                entry["error"] = str(exc)
                response_log.append(entry)

        def on_response(response: Response) -> None:
            task = asyncio.create_task(capture(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on("response", on_response)
        await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=90_000)
        try:
            await page.wait_for_function(
                r"""() => {
                  const text = document.body?.innerText || '';
                  return /신용융자\s*잔고/.test(text) && /[0-9]+(?:\.[0-9]+)?\s*조/.test(text);
                }""",
                timeout=60_000,
            )
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        await page.wait_for_timeout(5_000)

        body_text = await page.locator("body").inner_text()
        html = await page.content()
        meta_text = await page.evaluate(
            """() => Array.from(document.querySelectorAll('meta'))
              .map(meta => meta.content || '')
              .filter(Boolean)
              .join('\n')"""
        )
        inline_scripts = await page.evaluate(
            """() => Array.from(document.scripts)
              .map(script => script.textContent || '')
              .filter(text => text.length > 20 && /credit|deposit|신용|예탁/i.test(text))"""
        )
        text_payloads.extend([body_text, html, meta_text, *inline_scripts])
        for text in inline_scripts:
            try:
                payloads.append({"url": "inline-script", "data": json.loads(text)})
            except Exception:
                continue

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await browser.close()

    snapshot = extract_snapshot(body_text, meta_text, html)
    if not snapshot.date or snapshot.ratio is None:
        raise RuntimeError(
            "페이지에 표시된 최신 기준일 또는 신용비율을 읽지 못했습니다. "
            "debug_capture.json의 body_preview를 확인하세요."
        )

    scored_candidates: list[ScoredCandidate] = []
    seen_paths: set[str] = set()
    for payload in payloads:
        for path, rows in iter_arrays(payload["data"], payload["url"]):
            if raw := candidate_from_array(path, rows):
                normalized = normalize_candidate(raw, snapshot)
                if scored := score_series(raw.path, normalized, snapshot):
                    scored_candidates.append(scored)
                    seen_paths.add(raw.path)
        for path, rows in extract_parallel_arrays(payload["data"], payload["url"]):
            if path in seen_paths:
                continue
            if raw := candidate_from_array(path, rows):
                normalized = normalize_candidate(raw, snapshot)
                if scored := score_series(raw.path, normalized, snapshot):
                    scored_candidates.append(scored)

    scored_candidates.extend(build_separate_candidates(payloads, snapshot))
    scored_candidates.sort(key=lambda item: item.score, reverse=True)

    debug = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": SOURCE_URL,
        "visible_snapshot": snapshot.as_dict(),
        "candidate_count": len(scored_candidates),
        "top_candidates": [
            {"path": candidate.path, "score": round(candidate.score, 2), **candidate.details}
            for candidate in scored_candidates[:15]
        ],
        "responses": response_log,
        "body_preview": body_text[:12_000],
        "meta_preview": meta_text[:3_000],
    }
    DEBUG.write_text(json.dumps(debug, ensure_ascii=False, indent=2), encoding="utf-8")

    if not scored_candidates:
        raise RuntimeError("날짜·신용융자·고객예탁금이 함께 있는 시계열 후보를 찾지 못했습니다.")

    selected = scored_candidates[0]
    validate_selected(selected, snapshot)
    series = selected.series
    latest = series[-1]

    output = {
        "source": SOURCE_URL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "unit": "trillion_krw",
        "verified_against": snapshot.as_dict(),
        "latest": latest,
        "series": series,
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Saved {len(series)} rows to {OUTPUT}")
    print(f"Selected: {selected.path}")
    print(f"Visible snapshot: {snapshot.as_dict()}")
    print(
        f"Latest {latest['date']}: credit={latest['credit_trillion']}조, "
        f"deposit={latest['deposit_trillion']}조, ratio={latest['ratio']}%"
    )


if __name__ == "__main__":
    asyncio.run(main())
