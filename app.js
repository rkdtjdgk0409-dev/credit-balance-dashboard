const DATA_URL = "data.json?v=" + Date.now();

const $ = (id) => document.getElementById(id);

function finitePositive(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function normalizeRow(source) {
  if (!source) return null;
  const date = String(source.date || "");
  const credit = finitePositive(source.credit_trillion);
  const deposit = finitePositive(source.deposit_trillion);
  if (!/^20\d{2}-\d{2}-\d{2}$/.test(date) || credit === null || deposit === null) return null;
  return {
    date,
    credit_trillion: credit,
    deposit_trillion: deposit,
    ratio: (credit / deposit) * 100,
  };
}

function normalizeRows(source) {
  const byDate = new Map();
  for (const item of Array.isArray(source) ? source : []) {
    const row = normalizeRow(item);
    if (!row || row.ratio < 0.5 || row.ratio > 100) continue;
    byDate.set(row.date, row);
  }
  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
}

function fmtDate(value) {
  if (!value) return "—";
  const parsed = new Date(value + "T00:00:00+09:00");
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(parsed);
}

function fmtTrillion(value) {
  const number = finitePositive(value);
  if (number === null) return "—";
  return number.toLocaleString("ko-KR", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 2,
  }) + "조원";
}

function fmtPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—%";
  return number.toLocaleString("ko-KR", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }) + "%";
}

function ratioState(ratio) {
  if (!Number.isFinite(ratio)) {
    return {
      label: "데이터 확인 중",
      className: "neutral",
      description: "신용비율을 기준 구간과 비교하고 있습니다.",
    };
  }
  if (ratio < 25) {
    return {
      label: "매우 안정",
      className: "stable",
      description: "25% 미만으로 예탁금 대비 신용 부담이 낮은 구간입니다.",
    };
  }
  if (ratio < 35) {
    return {
      label: "일반 수준",
      className: "normal",
      description: "25% 이상 35% 미만으로 통상적인 신용 부담 구간입니다.",
    };
  }
  if (ratio < 40) {
    return {
      label: "주의 구간",
      className: "warning",
      description: "35% 이상 40% 미만으로 신용 부담과 반대매매 위험을 확인할 구간입니다.",
    };
  }
  return {
    label: "위험 구간",
    className: "danger",
    description: "40% 이상으로 레버리지 과열과 시장 충격 위험이 큰 구간입니다.",
  };
}

function setChange(latest, previous) {
  const element = $("ratioChange");
  if (!Number.isFinite(latest) || !Number.isFinite(previous)) {
    element.textContent = "전일 비교 없음";
    element.className = "ratio-change";
    return;
  }
  const delta = latest - previous;
  if (Math.abs(delta) < 0.005) {
    element.textContent = "전일과 동일";
    element.className = "ratio-change";
    return;
  }
  element.textContent =
    "전일 대비 " +
    (delta > 0 ? "▲ " : "▼ ") +
    Math.abs(delta).toFixed(2) +
    "%p";
  element.className = "ratio-change " + (delta > 0 ? "up" : "down");
}

function render(latest, rows, updatedAt) {
  const ratio = latest.ratio;
  const state = ratioState(ratio);

  $("ratioValue").textContent = fmtPercent(ratio);
  $("ratioState").textContent = state.label;
  $("ratioState").className = "state-pill " + state.className;
  $("stateDescription").textContent = state.description;
  $("latestCredit").textContent = fmtTrillion(latest.credit_trillion);
  $("latestDeposit").textContent = fmtTrillion(latest.deposit_trillion);
  $("asOfText").textContent =
    fmtDate(latest.date) +
    " 기준 · 고객예탁금은 공시 시차가 있을 수 있습니다.";

  const previous = rows.length >= 2 ? rows[rows.length - 2].ratio : NaN;
  setChange(ratio, previous);

  document.querySelectorAll(".guide-card").forEach((card) => {
    card.classList.toggle("current", card.dataset.state === state.className);
  });

  $("updatedAt").textContent = updatedAt
    ? "마지막 자동 수집: " +
      new Date(updatedAt).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })
    : "업데이트 정보 없음";
}

async function init() {
  try {
    const response = await fetch(DATA_URL, { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);

    const dataset = await response.json();
    const rows = normalizeRows(dataset.series);
    const latestFromFile = normalizeRow(dataset.latest);

    if (latestFromFile) {
      const sameDateIndex = rows.findIndex((row) => row.date === latestFromFile.date);
      if (sameDateIndex >= 0) rows[sameDateIndex] = latestFromFile;
      else rows.push(latestFromFile);
      rows.sort((a, b) => a.date.localeCompare(b.date));
    }

    const latest = rows.length ? rows[rows.length - 1] : latestFromFile;
    if (!latest) throw new Error("표시할 최신 데이터가 없습니다.");
    render(latest, rows, dataset.updated_at);
  } catch (error) {
    console.error(error);
    $("ratioState").textContent = "데이터 오류";
    $("ratioState").className = "state-pill danger";
    $("stateDescription").textContent =
      "data.json 또는 GitHub Actions 실행 기록을 확인해 주세요.";
    $("asOfText").textContent = error.message;
  }
}

window.addEventListener("DOMContentLoaded", init);
