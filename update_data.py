#!/usr/bin/env python3
"""Hang on! 신용잔고 페이지의 실제 네트워크 응답에서 날짜/신용/예탁금 시계열을 추출한다."""
from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from playwright.async_api import Response, async_playwright

SOURCE_URL = "https://www.hangon.co.kr/credit-balance"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data.json"
DEBUG = ROOT / "debug_capture.json"

DATE_KEYS = (
    "date", "day", "dt", "base_date", "baseDate", "basDt", "trdDd", "tradeDate",
    "일자", "날짜", "기준일", "기준일자",
)
CREDIT_PATTERNS = (
    "credit", "loan", "margin", "융자", "신용잔고", "신용융자", "creditbalance",
)
DEPOSIT_PATTERNS = (
    "deposit", "customer", "예탁", "고객예탁금", "investordeposit", "customerdeposit",
)
IGNORE_PATTERNS = ("ratio", "rate", "percent", "비율", "증감", "change")


def norm_key(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", str(value).lower())


def key_matches(key: str, patterns: Iterable[str]) -> bool:
    nk = norm_key(key)
    return any(norm_key(pattern) in nk for pattern in patterns)


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None

    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text or text in {"-", "—", "null", "None"}:
        return None

    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    total = 0.0
    matched = False
    units = (("조", 1e12), ("억", 1e8), ("만", 1e4))
    for unit, multiplier in units:
        m = re.search(rf"([0-9.]+){unit}", text)
        if m:
            total += float(m.group(1)) * multiplier
            matched = True
    if matched:
        return sign * total

    m = re.search(r"-?[0-9]+(?:\.[0-9]+)?", str(value).replace(",", ""))
    return float(m.group()) if m else None


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
                dt = datetime.strptime(candidate, fmt)
                if 2000 <= dt.year <= 2100:
                    return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
    return None


def iter_arrays(node: Any, path: str = "root"):
    if isinstance(node, list):
        if node and all(isinstance(item, dict) for item in node):
            yield path, node
        for i, item in enumerate(node):
            yield from iter_arrays(item, f"{path}[{i}]")
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from iter_arrays(value, f"{path}.{key}")


def find_key(row: dict[str, Any], patterns: Iterable[str], *, exclude: Iterable[str] = ()) -> str | None:
    scored: list[tuple[int, str]] = []
    for key in row:
        nk = norm_key(key)
        if any(norm_key(x) in nk for x in exclude):
            continue
        score = 0
        for pattern in patterns:
            np = norm_key(pattern)
            if nk == np:
                score = max(score, 10)
            elif np in nk:
                score = max(score, 5)
        if score:
            scored.append((score, key))
    return max(scored, default=(0, None))[1]


@dataclass
class Candidate:
    score: float
    path: str
    rows: list[dict[str, Any]]
    date_key: str
    credit_key: str
    deposit_key: str


def candidate_from_array(path: str, rows: list[dict[str, Any]]) -> Candidate | None:
    if len(rows) < 2:
        return None
    keys: dict[str, int] = {}
    for row in rows[:30]:
        for key in row:
            keys[key] = keys.get(key, 0) + 1
    representative = {key: None for key in keys}

    date_key = find_key(representative, DATE_KEYS)
    credit_key = find_key(representative, CREDIT_PATTERNS, exclude=IGNORE_PATTERNS + DEPOSIT_PATTERNS)
    deposit_key = find_key(representative, DEPOSIT_PATTERNS, exclude=IGNORE_PATTERNS)
    if not (date_key and credit_key and deposit_key) or credit_key == deposit_key:
        return None

    valid = 0
    for row in rows:
        if parse_date(row.get(date_key)) and parse_number(row.get(credit_key)) is not None and parse_number(row.get(deposit_key)) is not None:
            valid += 1
    if valid < 2:
        return None

    score = valid * 2 + min(len(rows), 200) * 0.1
    score += keys.get(date_key, 0) + keys.get(credit_key, 0) + keys.get(deposit_key, 0)
    return Candidate(score, path, rows, date_key, credit_key, deposit_key)


def choose_to_trillion(values: list[float], kind: str) -> float:
    finite = sorted(abs(v) for v in values if v and math.isfinite(v))
    if not finite:
        return 1.0
    median = finite[len(finite) // 2]
    target = 20.0 if kind == "credit" else 60.0
    plausible = (0.3, 150.0) if kind == "credit" else (2.0, 500.0)
    scales = [1.0, 1 / 10, 1 / 100, 1 / 1000, 1 / 10000, 1 / 1e8, 1 / 1e9, 1 / 1e12]
    ranked = []
    for scale in scales:
        result = median * scale
        penalty = abs(math.log10(max(result, 1e-12) / target))
        if not (plausible[0] <= result <= plausible[1]):
            penalty += 5
        ranked.append((penalty, scale))
    return min(ranked)[1]


def normalize_candidate(candidate: Candidate) -> list[dict[str, float | str]]:
    parsed = []
    for row in candidate.rows:
        date = parse_date(row.get(candidate.date_key))
        credit = parse_number(row.get(candidate.credit_key))
        deposit = parse_number(row.get(candidate.deposit_key))
        if date and credit is not None and deposit is not None and credit > 0 and deposit > 0:
            parsed.append((date, credit, deposit))

    credit_scale = choose_to_trillion([x[1] for x in parsed], "credit")
    deposit_scale = choose_to_trillion([x[2] for x in parsed], "deposit")

    by_date: dict[str, dict[str, float | str]] = {}
    for date, credit_raw, deposit_raw in parsed:
        credit = credit_raw * credit_scale
        deposit = deposit_raw * deposit_scale
        ratio = credit / deposit * 100 if deposit else None
        if not (0.05 <= credit <= 300 and 0.1 <= deposit <= 1000 and ratio and 0.1 <= ratio <= 300):
            continue
        by_date[date] = {
            "date": date,
            "credit_trillion": round(credit, 4),
            "deposit_trillion": round(deposit, 4),
            "ratio": round(ratio, 4),
        }
    return [by_date[key] for key in sorted(by_date)]



def primitive_list(node: Any) -> list[Any] | None:
    if isinstance(node, list) and node and all(not isinstance(x, (dict, list)) for x in node):
        return node
    return None


def extract_parallel_arrays(node: Any, path: str = "root") -> list[tuple[str, list[dict[str, Any]]]]:
    """labels/dates + credit[] + deposit[]처럼 병렬 배열로 내려오는 응답을 행 구조로 바꾼다."""
    found: list[tuple[str, list[dict[str, Any]]]] = []
    if isinstance(node, dict):
        primitive = {key: primitive_list(value) for key, value in node.items()}
        primitive = {key: value for key, value in primitive.items() if value is not None}
        date_key = next((key for key in primitive if key_matches(key, DATE_KEYS) or norm_key(key) in {"labels", "categories"}), None)
        credit_key = next((key for key in primitive if key_matches(key, CREDIT_PATTERNS) and not key_matches(key, IGNORE_PATTERNS + DEPOSIT_PATTERNS)), None)
        deposit_key = next((key for key in primitive if key_matches(key, DEPOSIT_PATTERNS) and not key_matches(key, IGNORE_PATTERNS)), None)
        if date_key and credit_key and deposit_key:
            dates, credits, deposits = primitive[date_key], primitive[credit_key], primitive[deposit_key]
            size = min(len(dates), len(credits), len(deposits))
            rows = [
                {"date": dates[i], "credit": credits[i], "deposit": deposits[i]}
                for i in range(size)
            ]
            if size >= 2:
                found.append((f"{path}:parallel", rows))

        # Chart.js: {labels: [...], datasets: [{label: '신용...', data:[...]}, ...]}
        labels = primitive.get("labels") or primitive.get("categories")
        datasets = node.get("datasets") or node.get("series")
        if labels and isinstance(datasets, list):
            credit_values = deposit_values = None
            for series in datasets:
                if not isinstance(series, dict):
                    continue
                label = series.get("label") or series.get("name") or series.get("title") or ""
                values = primitive_list(series.get("data")) or primitive_list(series.get("values"))
                if not values:
                    continue
                if key_matches(label, CREDIT_PATTERNS) and not key_matches(label, DEPOSIT_PATTERNS + IGNORE_PATTERNS):
                    credit_values = values
                if key_matches(label, DEPOSIT_PATTERNS) and not key_matches(label, IGNORE_PATTERNS):
                    deposit_values = values
            if credit_values and deposit_values:
                size = min(len(labels), len(credit_values), len(deposit_values))
                rows = [
                    {"date": labels[i], "credit": credit_values[i], "deposit": deposit_values[i]}
                    for i in range(size)
                ]
                if size >= 2:
                    found.append((f"{path}:chart-series", rows))

        for key, value in node.items():
            found.extend(extract_parallel_arrays(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found.extend(extract_parallel_arrays(value, f"{path}[{i}]"))
    return found


def extract_separate_series(payloads: list[dict[str, Any]]) -> list[dict[str, float | str]]:
    """날짜+신용 배열과 날짜+예탁금 배열이 서로 다른 응답/경로에 있을 때 날짜로 결합한다."""
    credit_series: list[tuple[int, dict[str, float]]] = []
    deposit_series: list[tuple[int, dict[str, float]]] = []

    for payload in payloads:
        for _, rows in iter_arrays(payload["data"], payload["url"]):
            if len(rows) < 2:
                continue
            all_keys = {key for row in rows[:30] for key in row}
            representative = {key: None for key in all_keys}
            date_key = find_key(representative, DATE_KEYS)
            if not date_key:
                continue
            credit_key = find_key(representative, CREDIT_PATTERNS, exclude=IGNORE_PATTERNS + DEPOSIT_PATTERNS)
            deposit_key = find_key(representative, DEPOSIT_PATTERNS, exclude=IGNORE_PATTERNS)
            if credit_key:
                values = {
                    date: value
                    for row in rows
                    if (date := parse_date(row.get(date_key))) and (value := parse_number(row.get(credit_key))) is not None and value > 0
                }
                if len(values) >= 2:
                    credit_series.append((len(values), values))
            if deposit_key:
                values = {
                    date: value
                    for row in rows
                    if (date := parse_date(row.get(date_key))) and (value := parse_number(row.get(deposit_key))) is not None and value > 0
                }
                if len(values) >= 2:
                    deposit_series.append((len(values), values))

    best: tuple[int, dict[str, float], dict[str, float]] | None = None
    for _, credits in credit_series:
        for _, deposits in deposit_series:
            overlap = set(credits) & set(deposits)
            if len(overlap) >= 2 and (best is None or len(overlap) > best[0]):
                best = (len(overlap), credits, deposits)
    if best is None:
        return []

    _, credits, deposits = best
    dates = sorted(set(credits) & set(deposits))
    credit_scale = choose_to_trillion([credits[d] for d in dates], "credit")
    deposit_scale = choose_to_trillion([deposits[d] for d in dates], "deposit")
    result = []
    for date in dates:
        credit = credits[date] * credit_scale
        deposit = deposits[date] * deposit_scale
        ratio = credit / deposit * 100
        if 0.05 <= credit <= 300 and 0.1 <= deposit <= 1000 and 0.1 <= ratio <= 300:
            result.append({
                "date": date,
                "credit_trillion": round(credit, 4),
                "deposit_trillion": round(deposit, 4),
                "ratio": round(ratio, 4),
            })
    return result

def regex_fallback(texts: list[str]) -> list[dict[str, float | str]]:
    # HTML/스크립트 내에 날짜와 두 수치가 한 줄에 직렬화되어 있을 때 사용하는 마지막 보조 수단.
    joined = "\n".join(texts)
    pattern = re.compile(
        r"(?P<date>20\d{2}[-./]?\d{2}[-./]?\d{2}).{0,220}?"
        r"(?P<credit>[0-9][0-9,.]{2,}).{0,220}?"
        r"(?P<deposit>[0-9][0-9,.]{2,})",
        re.S,
    )
    raw = []
    for match in pattern.finditer(joined):
        date = parse_date(match.group("date"))
        credit = parse_number(match.group("credit"))
        deposit = parse_number(match.group("deposit"))
        if date and credit and deposit:
            raw.append((date, credit, deposit))
    if len(raw) < 2:
        return []
    credit_scale = choose_to_trillion([r[1] for r in raw], "credit")
    deposit_scale = choose_to_trillion([r[2] for r in raw], "deposit")
    result = []
    for date, c, d in raw:
        c *= credit_scale
        d *= deposit_scale
        ratio = c / d * 100
        if 0.1 < ratio < 300:
            result.append({"date": date, "credit_trillion": round(c, 4), "deposit_trillion": round(d, 4), "ratio": round(ratio, 4)})
    return list({row["date"]: row for row in result}.values())


async def main() -> None:
    payloads: list[dict[str, Any]] = []
    text_payloads: list[str] = []
    response_log: list[dict[str, Any]] = []
    tasks: set[asyncio.Task] = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        async def capture(response: Response) -> None:
            try:
                content_type = (response.headers.get("content-type") or "").lower()
                url = response.url
                entry = {"url": url, "status": response.status, "content_type": content_type}
                if "json" in content_type or response.request.resource_type in {"xhr", "fetch"}:
                    body = await response.text()
                    entry["size"] = len(body)
                    text_payloads.append(body[:2_000_000])
                    try:
                        payload = json.loads(body)
                        payloads.append({"url": url, "data": payload})
                        entry["json"] = True
                    except json.JSONDecodeError:
                        entry["json"] = False
                response_log.append(entry)
            except Exception as exc:  # 네트워크 응답 하나의 실패가 전체 수집을 막지 않게 함
                response_log.append({"url": response.url, "error": str(exc)})

        def on_response(response: Response) -> None:
            task = asyncio.create_task(capture(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on("response", on_response)
        await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=90_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=45_000)
        except Exception:
            pass
        await page.wait_for_timeout(8_000)
        body_text = await page.locator("body").inner_text()
        html = await page.content()
        text_payloads.extend([body_text, html])

        # Next.js에 인라인으로 포함된 상태 데이터도 수집한다.
        inline_json = await page.evaluate("""
          () => Array.from(document.scripts)
            .map(s => s.textContent || '')
            .filter(t => t.length > 20 && (t.includes('credit') || t.includes('deposit') || t.includes('예탁') || t.includes('신용')))
        """)
        text_payloads.extend(inline_json)
        for text in inline_json:
            try:
                payloads.append({"url": "inline-script", "data": json.loads(text)})
            except Exception:
                pass

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await browser.close()

    candidates: list[Candidate] = []
    for payload in payloads:
        for path, rows in iter_arrays(payload["data"], payload["url"]):
            candidate = candidate_from_array(path, rows)
            if candidate:
                candidates.append(candidate)
        for path, rows in extract_parallel_arrays(payload["data"], payload["url"]):
            candidate = candidate_from_array(path, rows)
            if candidate:
                candidates.append(candidate)

    series: list[dict[str, float | str]] = []
    selected: dict[str, Any] | None = None
    for candidate in sorted(candidates, key=lambda x: x.score, reverse=True):
        normalized = normalize_candidate(candidate)
        if len(normalized) >= 2:
            series = normalized
            selected = {
                "path": candidate.path,
                "score": candidate.score,
                "date_key": candidate.date_key,
                "credit_key": candidate.credit_key,
                "deposit_key": candidate.deposit_key,
                "rows": len(series),
            }
            break

    if not series:
        series = extract_separate_series(payloads)
        if series:
            selected = {"method": "merged_separate_series", "rows": len(series)}

    if not series:
        series = sorted(regex_fallback(text_payloads), key=lambda row: row["date"])
        if series:
            selected = {"method": "regex_fallback", "rows": len(series)}

    debug = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": SOURCE_URL,
        "selected": selected,
        "candidate_count": len(candidates),
        "responses": response_log,
        "body_preview": body_text[:5000],
    }
    DEBUG.write_text(json.dumps(debug, ensure_ascii=False, indent=2), encoding="utf-8")

    if len(series) < 2:
        raise RuntimeError(
            "신용융자 잔고와 고객예탁금 시계열을 찾지 못했습니다. "
            "Actions 실행 결과의 debug_capture.json 아티팩트를 확인하세요."
        )

    output = {
        "source": SOURCE_URL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "unit": "trillion_krw",
        "series": series,
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(series)} rows to {OUTPUT}")
    print(f"Selected: {selected}")
    latest = series[-1]
    print(
        f"Latest {latest['date']}: credit={latest['credit_trillion']}조, "
        f"deposit={latest['deposit_trillion']}조, ratio={latest['ratio']}%"
    )


if __name__ == "__main__":
    asyncio.run(main())
