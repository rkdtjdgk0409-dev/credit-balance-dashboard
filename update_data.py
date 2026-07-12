#!/usr/bin/env python3
"""Hang on 신용잔고 페이지에서 검증된 최신값과 가능한 전체 시계열을 수집한다.

수집 우선순위
1) 네트워크/Next.js payload에서 전체 날짜별 시계열을 찾는다.
2) 전체 시계열이 감춰져 있거나 구조가 바뀌면, 화면에 표시된 최신값을 기존 data.json에 날짜별로 누적한다.

따라서 사이트 내부 차트 구조 변경만으로 GitHub Actions가 실패하지 않는다.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from playwright.async_api import Response, async_playwright

SOURCE_URL = "https://www.hangon.co.kr/credit-balance"
ROOT = Path(os.environ.get("GITHUB_WORKSPACE", Path(__file__).resolve().parent)).resolve()
OUTPUT = ROOT / "data.json"
DEBUG = ROOT / "debug_capture.json"

DATE_KEYS = (
    "date", "day", "dt", "base_date", "baseDate", "basDt", "trdDd", "tradeDate",
    "trade_date", "bizDate", "businessDate", "stck_bsop_date", "일자", "날짜", "기준일", "기준일자",
)
CREDIT_PATTERNS = (
    "credit", "creditbalance", "creditloan", "credit_loan", "creditamount", "crdt",
    "loanbalance", "marginloan", "융자", "융자잔고", "신용잔고", "신용융자", "신용거래융자",
)
DEPOSIT_PATTERNS = (
    "deposit", "customerdeposit", "investordeposit", "customer_deposit", "investor_deposit",
    "custdps", "예탁", "예탁금", "고객예탁금", "투자자예탁금", "예수금",
)
IGNORE_PATTERNS = (
    "ratio", "rate", "percent", "percentage", "비율", "증감", "change", "changerate",
)
VALUE_KEYS = ("y", "value", "val", "amount", "balance", "data", "close")
SCALES_TO_TRILLION = tuple(10.0 ** (-power) for power in range(0, 13))


def norm_key(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", str(value).lower())


def matches(value: Any, patterns: Iterable[str]) -> bool:
    normalized = norm_key(value)
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
    matched_unit = False
    for unit, multiplier in (("조", 1e12), ("억", 1e8), ("만", 1e4)):
        match = re.search(rf"([0-9]+(?:\.[0-9]+)?){unit}", unsigned)
        if match:
            total += float(match.group(1)) * multiplier
            matched_unit = True
    if matched_unit:
        return sign * total

    match = re.search(r"-?[0-9]+(?:\.[0-9]+)?", original.replace(",", ""))
    return float(match.group()) if match else None


def parse_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        number = float(value)
        try:
            if 946684800 <= number <= 4102444800:
                return datetime.fromtimestamp(number, tz=timezone.utc).strftime("%Y-%m-%d")
            if 946684800000 <= number <= 4102444800000:
                return datetime.fromtimestamp(number / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            pass

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


@dataclass(frozen=True)
class Snapshot:
    date: str | None
    ratio: float | None
    credit_trillion: float | None
    deposit_trillion: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "ratio": self.ratio,
            "credit_trillion": self.credit_trillion,
            "deposit_trillion": self.deposit_trillion,
        }


def first_reasonable(values: Iterable[float | None], low: float, high: float) -> float | None:
    for value in values:
        if value is not None and low <= value <= high:
            return value
    return None


def extract_snapshot(*texts: str) -> Snapshot:
    joined = "\n".join(text for text in texts if text)
    compact = re.sub(r"[\t\r]+", " ", joined)

    ratio_values: list[float | None] = []
    for pattern in (
        r"예탁금\s*대비\s*신용(?:\s*비율)?[^0-9]{0,80}([0-9]+(?:\.[0-9]+)?)\s*%",
        r"신용\s*비율[^0-9]{0,50}([0-9]+(?:\.[0-9]+)?)\s*%",
    ):
        ratio_values.extend(parse_number(value) for value in re.findall(pattern, compact, flags=re.I | re.S))
    ratio = first_reasonable(ratio_values, 5.0, 80.0)

    date_value = None
    for pattern in (
        r"기준\s*(20\d{2}[-./]\d{1,2}[-./]\d{1,2})",
        r"(20\d{2}[-./]\d{1,2}[-./]\d{1,2})\s*기준",
    ):
        match = re.search(pattern, compact)
        if match:
            date_value = parse_date(match.group(1))
            if date_value:
                break

    def labelled_trillion(labels: tuple[str, ...], low: float, high: float) -> float | None:
        values: list[float | None] = []
        label_expr = "|".join(labels)
        for match in re.finditer(
            rf"(?:{label_expr})[\s\S]{{0,120}}?([0-9]+(?:\.[0-9]+)?)\s*조(?:원)?",
            compact,
            flags=re.I,
        ):
            values.append(parse_number(match.group(1)))
        return first_reasonable(values, low, high)

    credit = labelled_trillion(
        (r"신용융자\s*잔고", r"신용\s*잔고", r"신용거래융자", r"Credit"),
        1.0,
        100.0,
    )
    deposit = labelled_trillion(
        (r"고객예탁금", r"투자자예탁금", r"Deposit"),
        10.0,
        300.0,
    )

    if credit is not None and deposit is not None:
        calculated = credit / deposit * 100
        if ratio is None or abs(calculated - ratio) <= 1.5:
            ratio = calculated

    return Snapshot(
        date=date_value,
        ratio=round(ratio, 4) if ratio is not None else None,
        credit_trillion=round(credit, 4) if credit is not None else None,
        deposit_trillion=round(deposit, 4) if deposit is not None else None,
    )


def find_key(keys: Iterable[str], patterns: Iterable[str], exclude: Iterable[str] = ()) -> str | None:
    ranked: list[tuple[int, str]] = []
    for key in keys:
        normalized = norm_key(key)
        if any(norm_key(item) in normalized for item in exclude):
            continue
        score = 0
        for pattern in patterns:
            p = norm_key(pattern)
            if normalized == p:
                score = max(score, 20)
            elif normalized.startswith(p) or normalized.endswith(p):
                score = max(score, 12)
            elif p in normalized:
                score = max(score, 7)
        if score:
            ranked.append((score, key))
    return max(ranked, default=(0, None))[1]


def primitive_list(value: Any) -> list[Any] | None:
    if isinstance(value, list) and value and all(not isinstance(item, (dict, list)) for item in value):
        return value
    return None


def decode_text_payloads(text: str, source: str) -> list[dict[str, Any]]:
    if not text:
        return []
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    queue: list[tuple[str, str]] = [(source, text), (f"{source}:html", html_lib.unescape(text))]

    flight_pattern = re.compile(
        r'(?:self\.)?__next_f\.push\(\[\s*\d+\s*,\s*("(?:\\.|[^"\\])*")\s*\]\)',
        flags=re.S,
    )
    for match in flight_pattern.finditer(text):
        try:
            decoded = json.loads(match.group(1))
            if isinstance(decoded, str):
                queue.append((f"{source}:next-flight", decoded))
        except Exception:
            continue

    decoder = json.JSONDecoder()
    while queue and len(results) < 1000:
        label, candidate = queue.pop(0)
        candidate = candidate[:8_000_000]
        try:
            value = json.loads(candidate)
            fingerprint = repr(value)[:4000]
            if fingerprint not in seen:
                seen.add(fingerprint)
                results.append({"url": label, "data": value})
            if isinstance(value, str) and value != candidate:
                queue.append((f"{label}:nested", value))
        except Exception:
            pass

        for start_match in list(re.finditer(r"[\[{]", candidate))[:5000]:
            start = start_match.start()
            try:
                value, _ = decoder.raw_decode(candidate[start:])
            except Exception:
                continue
            if not isinstance(value, (dict, list)):
                continue
            fingerprint = repr(value)[:4000]
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            results.append({"url": f"{label}:fragment@{start}", "data": value})
            if len(results) >= 1000:
                break
    return results


def iter_nodes(node: Any, path: str = "root"):
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from iter_nodes(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_nodes(value, f"{path}[{index}]")


def choose_scale(values: list[float], target: float | None, kind: str) -> float:
    finite = sorted(abs(value) for value in values if value and math.isfinite(value))
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
            penalty += 10
        ranked.append((penalty, scale))
    return min(ranked)[1]


def normalize_rows(
    rows: list[tuple[str, float, float]], snapshot: Snapshot
) -> list[dict[str, float | str]]:
    if len(rows) < 2:
        return []
    reference = next((row for row in rows if snapshot.date and row[0] == snapshot.date), rows[-1])
    credit_scale = choose_scale([reference[1]], snapshot.credit_trillion, "credit")
    deposit_scale = choose_scale([reference[2]], snapshot.deposit_trillion, "deposit")

    by_date: dict[str, dict[str, float | str]] = {}
    for date_value, raw_credit, raw_deposit in rows:
        credit = raw_credit * credit_scale
        deposit = raw_deposit * deposit_scale
        ratio = credit / deposit * 100
        if not (0.5 <= credit <= 150 and 5 <= deposit <= 500 and 0.5 <= ratio <= 100):
            continue
        by_date[date_value] = {
            "date": date_value,
            "credit_trillion": round(credit, 4),
            "deposit_trillion": round(deposit, 4),
            "ratio": round(ratio, 4),
        }
    return [by_date[key] for key in sorted(by_date)]


def rows_from_object_array(array: list[Any], snapshot: Snapshot) -> list[dict[str, float | str]]:
    if len(array) < 2 or not all(isinstance(item, dict) for item in array):
        return []
    keys = {key for row in array[:100] for key in row}
    date_key = find_key(keys, DATE_KEYS)
    credit_key = find_key(keys, CREDIT_PATTERNS, IGNORE_PATTERNS + DEPOSIT_PATTERNS)
    deposit_key = find_key(keys, DEPOSIT_PATTERNS, IGNORE_PATTERNS)
    if not date_key or not credit_key or not deposit_key or credit_key == deposit_key:
        return []

    parsed: list[tuple[str, float, float]] = []
    for row in array:
        date_value = parse_date(row.get(date_key))
        credit = parse_number(row.get(credit_key))
        deposit = parse_number(row.get(deposit_key))
        if date_value and credit is not None and deposit is not None and credit > 0 and deposit > 0:
            parsed.append((date_value, credit, deposit))
    return normalize_rows(parsed, snapshot)


def point_map(item: dict[str, Any]) -> dict[str, float]:
    data = item.get("data") or item.get("values") or item.get("points")
    if not isinstance(data, list) or not data or not all(isinstance(row, dict) for row in data):
        return {}
    keys = {key for row in data[:100] for key in row}
    date_key = next(
        (key for key in keys if matches(key, DATE_KEYS) or norm_key(key) in {"x", "category", "label", "name"}),
        None,
    )
    value_key = next((key for key in keys if norm_key(key) in VALUE_KEYS), None)
    if not date_key or not value_key:
        return {}
    output: dict[str, float] = {}
    for row in data:
        date_value = parse_date(row.get(date_key))
        value = parse_number(row.get(value_key))
        if date_value and value is not None and value > 0:
            output[date_value] = value
    return output


def find_date_labels(node: Any) -> list[Any] | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if norm_key(key) in {"labels", "categories", "dates", "xaxisdata"}:
                values = primitive_list(value)
                if values and sum(parse_date(item) is not None for item in values) >= 2:
                    return values
        for value in node.values():
            found = find_date_labels(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_date_labels(value)
            if found:
                return found
    return None


def rows_from_named_series(node: dict[str, Any], snapshot: Snapshot) -> list[dict[str, float | str]]:
    series_lists = [
        node.get(key) for key in ("series", "datasets", "chartData", "dataSeries")
        if isinstance(node.get(key), list)
    ]
    labels = find_date_labels(node)
    for series in series_lists:
        credit_map: dict[str, float] = {}
        deposit_map: dict[str, float] = {}
        for item in series:
            if not isinstance(item, dict):
                continue
            label = str(item.get("name") or item.get("label") or item.get("title") or item.get("key") or "")
            is_credit = matches(label, CREDIT_PATTERNS) and not matches(label, DEPOSIT_PATTERNS + IGNORE_PATTERNS)
            is_deposit = matches(label, DEPOSIT_PATTERNS) and not matches(label, IGNORE_PATTERNS)
            if not (is_credit or is_deposit):
                continue

            mapped = point_map(item)
            if not mapped and labels:
                values = primitive_list(item.get("data")) or primitive_list(item.get("values"))
                if values:
                    for raw_date, raw_value in zip(labels, values):
                        date_value = parse_date(raw_date)
                        value = parse_number(raw_value)
                        if date_value and value is not None and value > 0:
                            mapped[date_value] = value
            if is_credit:
                credit_map.update(mapped)
            if is_deposit:
                deposit_map.update(mapped)

        common = sorted(set(credit_map) & set(deposit_map))
        if len(common) >= 2:
            parsed = [(day, credit_map[day], deposit_map[day]) for day in common]
            normalized = normalize_rows(parsed, snapshot)
            if normalized:
                return normalized
    return []


def candidate_score(series: list[dict[str, float | str]], snapshot: Snapshot) -> float:
    if len(series) < 2:
        return -1e9
    latest = series[-1]
    score = min(len(series), 500) * 2
    if snapshot.date:
        distance = abs((date.fromisoformat(str(latest["date"])) - date.fromisoformat(snapshot.date)).days)
        score += 20_000 if distance == 0 else max(-30_000, 8_000 - distance * 500)
    if snapshot.ratio is not None:
        score += max(-10_000, 8_000 - abs(float(latest["ratio"]) - snapshot.ratio) * 4000)
    for field, target in (
        ("credit_trillion", snapshot.credit_trillion),
        ("deposit_trillion", snapshot.deposit_trillion),
    ):
        if target is not None:
            error = abs(float(latest[field]) - target) / target
            score += max(-8_000, 5_000 - error * 50_000)
    return score


def validate_candidate(series: list[dict[str, float | str]], snapshot: Snapshot) -> None:
    latest = series[-1]
    problems: list[str] = []
    if snapshot.date:
        distance = abs((date.fromisoformat(str(latest["date"])) - date.fromisoformat(snapshot.date)).days)
        if distance > 3:
            problems.append(f"날짜 불일치: 화면 {snapshot.date}, 후보 {latest['date']}")
    if snapshot.ratio is not None and abs(float(latest["ratio"]) - snapshot.ratio) > 0.7:
        problems.append(f"비율 불일치: 화면 {snapshot.ratio:.2f}%, 후보 {float(latest['ratio']):.2f}%")
    for field, target, label in (
        ("credit_trillion", snapshot.credit_trillion, "신용융자"),
        ("deposit_trillion", snapshot.deposit_trillion, "고객예탁금"),
    ):
        if target is not None and abs(float(latest[field]) - target) > max(0.5, target * 0.03):
            problems.append(f"{label} 불일치")
    if problems:
        raise RuntimeError(" / ".join(problems))


def snapshot_row(snapshot: Snapshot) -> dict[str, float | str]:
    if not snapshot.date or snapshot.credit_trillion is None or snapshot.deposit_trillion is None:
        raise RuntimeError("페이지 화면에서 기준일·신용융자 잔고·고객예탁금을 모두 읽지 못했습니다.")
    credit = float(snapshot.credit_trillion)
    deposit = float(snapshot.deposit_trillion)
    return {
        "date": snapshot.date,
        "credit_trillion": round(credit, 4),
        "deposit_trillion": round(deposit, 4),
        "ratio": round(credit / deposit * 100, 4),
    }


def load_existing_series() -> list[dict[str, float | str]]:
    if not OUTPUT.exists():
        return []
    try:
        payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception:
        return []
    source: list[Any] = payload.get("series", []) if isinstance(payload, dict) else []
    if not isinstance(source, list):
        source = []
    if isinstance(payload, dict) and isinstance(payload.get("latest"), dict):
        source = [*source, payload["latest"]]

    by_date: dict[str, dict[str, float | str]] = {}
    for row in source:
        if not isinstance(row, dict):
            continue
        date_value = parse_date(row.get("date"))
        credit = parse_number(row.get("credit_trillion"))
        deposit = parse_number(row.get("deposit_trillion"))
        if not date_value or credit is None or deposit is None or credit <= 0 or deposit <= 0:
            continue
        if not (0.5 <= credit <= 150 and 5 <= deposit <= 500):
            continue
        by_date[date_value] = {
            "date": date_value,
            "credit_trillion": round(credit, 4),
            "deposit_trillion": round(deposit, 4),
            "ratio": round(credit / deposit * 100, 4),
        }
    return [by_date[key] for key in sorted(by_date)][-520:]


def append_snapshot(existing: list[dict[str, float | str]], snapshot: Snapshot) -> list[dict[str, float | str]]:
    by_date = {str(row["date"]): row for row in existing}
    latest = snapshot_row(snapshot)
    by_date[str(latest["date"])] = latest
    return [by_date[key] for key in sorted(by_date)][-520:]


async def main() -> None:
    payloads: list[dict[str, Any]] = []
    text_payloads: list[tuple[str, str]] = []
    responses: list[dict[str, Any]] = []
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
                    response.request.resource_type in {"xhr", "fetch", "document", "script"}
                    or "json" in entry["content_type"]
                    or "text/x-component" in entry["content_type"]
                )
                if should_read and response.status < 400:
                    text = await response.text()
                    entry["size"] = len(text)
                    text_payloads.append((response.url, text[:5_000_000]))
                    try:
                        payloads.append({"url": response.url, "data": json.loads(text)})
                        entry["json"] = True
                    except Exception:
                        entry["json"] = False
            except Exception as exc:
                entry["error"] = str(exc)
            responses.append(entry)

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
                  return /신용융자\s*잔고/.test(text) && /고객예탁금/.test(text) && /조/.test(text);
                }""",
                timeout=60_000,
            )
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        await page.wait_for_timeout(4_000)

        body_text = await page.locator("body").inner_text()
        html = await page.content()
        meta_text = await page.evaluate(
            """() => Array.from(document.querySelectorAll('meta'))
              .map(meta => meta.content || '')
              .filter(Boolean)
              .join(String.fromCharCode(10))"""
        )
        inline_scripts = await page.evaluate(
            """() => Array.from(document.scripts)
              .map(script => script.textContent || '')
              .filter(text => text.length > 20)"""
        )
        text_payloads.extend([
            ("body", body_text),
            ("html", html),
            ("meta", meta_text),
            *[(f"inline-{index}", text) for index, text in enumerate(inline_scripts)],
        ])

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await browser.close()

    snapshot = extract_snapshot(body_text, meta_text, html)
    if not snapshot.date or snapshot.credit_trillion is None or snapshot.deposit_trillion is None:
        debug = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source": SOURCE_URL,
            "visible_snapshot": snapshot.as_dict(),
            "responses": responses,
            "body_preview": body_text[:20_000],
        }
        DEBUG.write_text(json.dumps(debug, ensure_ascii=False, indent=2), encoding="utf-8")
        raise RuntimeError("페이지 화면의 기준일·신용융자·고객예탁금을 읽지 못했습니다.")

    for source, text in text_payloads:
        payloads.extend(decode_text_payloads(text, source))

    candidates: list[tuple[float, str, list[dict[str, float | str]]]] = []
    seen: set[str] = set()
    for payload in payloads:
        for path, node in iter_nodes(payload.get("data"), payload.get("url", "payload")):
            series: list[dict[str, float | str]] = []
            if isinstance(node, list):
                series = rows_from_object_array(node, snapshot)
            elif isinstance(node, dict):
                series = rows_from_named_series(node, snapshot)
            if len(series) < 2:
                continue
            fingerprint = json.dumps(series[-5:], ensure_ascii=False, sort_keys=True)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            candidates.append((candidate_score(series, snapshot), path, series))

    candidates.sort(key=lambda item: item[0], reverse=True)
    collection_mode = "snapshot_append"
    selected_path = None
    fallback_reason = "전체 시계열 후보를 찾지 못함"

    if candidates:
        _, selected_path, selected_series = candidates[0]
        try:
            validate_candidate(selected_series, snapshot)
            series = selected_series[-520:]
            collection_mode = "full_series"
            fallback_reason = None
        except Exception as exc:
            fallback_reason = str(exc)
            series = append_snapshot(load_existing_series(), snapshot)
    else:
        series = append_snapshot(load_existing_series(), snapshot)

    latest = series[-1]
    output = {
        "source": SOURCE_URL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "unit": "trillion_krw",
        "collection_mode": collection_mode,
        "message": (
            "Hang on 전체 시계열을 추출했습니다."
            if collection_mode == "full_series"
            else "Hang on 화면에 표시된 최신 검증값을 날짜별로 누적했습니다."
        ),
        "fallback_reason": fallback_reason,
        "verified_against": snapshot.as_dict(),
        "latest": latest,
        "series": series,
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    debug = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": SOURCE_URL,
        "visible_snapshot": snapshot.as_dict(),
        "collection_mode": collection_mode,
        "selected_path": selected_path,
        "fallback_reason": fallback_reason,
        "candidate_count": len(candidates),
        "top_candidates": [
            {
                "score": round(score, 2),
                "path": path,
                "rows": len(candidate),
                "latest": candidate[-1],
            }
            for score, path, candidate in candidates[:15]
        ],
        "payload_count": len(payloads),
        "captured_text_count": len(text_payloads),
        "responses": responses,
        "body_preview": body_text[:20_000],
    }
    DEBUG.write_text(json.dumps(debug, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Saved {len(series)} rows to {OUTPUT}")
    print(f"Collection mode: {collection_mode}")
    print(f"Visible snapshot: {snapshot.as_dict()}")
    print(f"Latest: {latest}")
    if fallback_reason:
        print(f"Fallback reason: {fallback_reason}")


if __name__ == "__main__":
    asyncio.run(main())
