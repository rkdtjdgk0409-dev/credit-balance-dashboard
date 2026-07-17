const $ = (id) => document.getElementById(id);
const DATA_URL = "data.json?v=" + Date.now();

function row(value) {
  if (!value) return null;
  const credit = Number(value.credit_trillion);
  const deposit = Number(value.deposit_trillion);
  const date = String(value.date || "");
  if (!Number.isFinite(credit) || !Number.isFinite(deposit) || deposit <= 0) return null;
  return { date, credit, deposit, ratio: credit / deposit * 100 };
}

function status(ratio) {
  if (ratio < 25) return ["매우 안정", "stable"];
  if (ratio < 35) return ["일반 수준", "normal"];
  if (ratio < 40) return ["주의 구간", "warning"];
  return ["위험 구간", "danger"];
}

function money(value) {
  return Number(value).toLocaleString("ko-KR", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 2
  }) + "조원";
}

async function init() {
  try {
    const response = await fetch(DATA_URL, { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    const rows = (Array.isArray(data.series) ? data.series : []).map(row).filter(Boolean);
    const latestFile = row(data.latest);
    if (latestFile) {
      const index = rows.findIndex((item) => item.date === latestFile.date);
      if (index >= 0) rows[index] = latestFile;
      else rows.push(latestFile);
    }
    rows.sort((a, b) => a.date.localeCompare(b.date));
    const latest = rows[rows.length - 1] || latestFile;
    if (!latest) throw new Error("표시할 데이터가 없습니다.");

    const state = status(latest.ratio);
    $("ratioValue").textContent = latest.ratio.toFixed(1) + "%";
    $("ratioState").textContent = state[0];
    $("ratioState").className = "state " + state[1];
    $("latestCredit").textContent = money(latest.credit);
    $("latestDeposit").textContent = money(latest.deposit);
    $("asOfText").textContent = latest.date + " 기준";

    const previous = rows.length > 1 ? rows[rows.length - 2].ratio : NaN;
    if (Number.isFinite(previous)) {
      const delta = latest.ratio - previous;
      $("ratioChange").textContent =
        (delta >= 0 ? "▲ " : "▼ ") + Math.abs(delta).toFixed(2) + "%p";
      $("ratioChange").className = "change " + (delta > 0 ? "up" : delta < 0 ? "down" : "");
    }

    document.querySelectorAll(".guide-card").forEach((card) => {
      card.classList.toggle("current", card.dataset.state === state[1]);
    });

    $("updatedAt").textContent = data.updated_at
      ? "마지막 갱신 " + new Date(data.updated_at).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })
      : "업데이트 정보 없음";
  } catch (error) {
    $("ratioState").textContent = "데이터 오류";
    $("ratioState").className = "state danger";
    $("asOfText").textContent = error.message;
  }
}

window.addEventListener("DOMContentLoaded", init);
