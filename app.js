const DATA_URL = `data.json?v=${Date.now()}`;
let chart = null;
let currentView = "ratio";
let dataset = null;
let rows = [];

const $ = (id) => document.getElementById(id);

function finitePositive(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function normalizeRows(source) {
  const byDate = new Map();
  for (const item of Array.isArray(source) ? source : []) {
    const date = String(item?.date || "");
    const credit = finitePositive(item?.credit_trillion);
    const deposit = finitePositive(item?.deposit_trillion);
    if (!/^20\d{2}-\d{2}-\d{2}$/.test(date) || credit === null || deposit === null) continue;
    if (credit > 150 || deposit > 500) continue;
    const ratio = (credit / deposit) * 100;
    if (ratio < 0.5 || ratio > 100) continue;
    byDate.set(date, {
      date,
      credit_trillion: credit,
      deposit_trillion: deposit,
      ratio,
    });
  }
  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
}

function sixMonthRows(source) {
  if (!Array.isArray(source) || source.length === 0) return [];
  const latest = new Date(`${source.at(-1).date}T00:00:00+09:00`);
  if (Number.isNaN(latest.getTime())) return source;
  const cutoff = new Date(latest.getTime());
  const targetMonth = cutoff.getMonth() - 6;
  cutoff.setDate(1);
  cutoff.setMonth(targetMonth);
  const latestDay = latest.getDate();
  const lastDay = new Date(cutoff.getFullYear(), cutoff.getMonth() + 1, 0).getDate();
  cutoff.setDate(Math.min(latestDay, lastDay));
  return source.filter((row) => new Date(`${row.date}T00:00:00+09:00`) >= cutoff);
}

function normalizeLatest(source) {
  if (!source) return null;
  const credit = finitePositive(source.credit_trillion);
  const deposit = finitePositive(source.deposit_trillion);
  const date = String(source.date || "");
  if (credit === null || deposit === null || !/^20\d{2}-\d{2}-\d{2}$/.test(date)) return null;
  return { date, credit_trillion: credit, deposit_trillion: deposit, ratio: (credit / deposit) * 100 };
}

function fmtDate(value) {
  if (!value) return "—";
  const parsed = new Date(`${value}T00:00:00+09:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(parsed);
}

function shortDate(value, includeYear = false) {
  const parsed = new Date(`${value}T00:00:00+09:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  return includeYear
    ? `${String(parsed.getFullYear()).slice(2)}.${parsed.getMonth() + 1}.${parsed.getDate()}`
    : `${parsed.getMonth() + 1}.${parsed.getDate()}`;
}

function fmtTrillion(value) {
  const number = finitePositive(value);
  if (number === null) return "—";
  return `${number.toLocaleString("ko-KR", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 2,
  })}조원`;
}

function fmtPercent(value, digits = 1) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—%";
  return `${number.toLocaleString("ko-KR", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}%`;
}

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function ratioState(ratio) {
  if (!Number.isFinite(ratio)) return { label: "데이터 확인 중", className: "neutral" };
  if (ratio < 25) return { label: "매우 안정", className: "stable" };
  if (ratio < 35) return { label: "일반 수준", className: "normal" };
  if (ratio < 40) return { label: "주의 구간", className: "warning" };
  return { label: "위험 구간", className: "danger" };
}

function setChange(latest, previous) {
  const element = $("ratioChange");
  if (!Number.isFinite(latest) || !Number.isFinite(previous)) {
    element.textContent = "전일 비교 없음";
    element.className = "change-pill neutral";
    return;
  }
  const delta = latest - previous;
  if (Math.abs(delta) < 0.005) {
    element.textContent = "전일과 동일";
    element.className = "change-pill neutral";
    return;
  }
  element.textContent = `${delta > 0 ? "▲" : "▼"} ${Math.abs(delta).toFixed(2)}%p`;
  element.className = `change-pill ${delta > 0 ? "up" : "down"}`;
}

function commonOptions(view) {
  const options = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    animation: { duration: 350 },
    plugins: {
      legend: {
        display: view === "compare",
        position: "top",
        align: "start",
        labels: {
          color: css("--muted"),
          usePointStyle: true,
          pointStyle: "line",
          boxWidth: 18,
          boxHeight: 6,
          padding: 18,
        },
      },
      tooltip: {
        backgroundColor: "rgba(8, 11, 17, 0.97)",
        borderColor: "rgba(255,255,255,.13)",
        borderWidth: 1,
        titleColor: css("--text"),
        bodyColor: css("--text"),
        padding: 12,
        callbacks: {
          title(items) {
            const index = items?.[0]?.dataIndex;
            return Number.isInteger(index) ? fmtDate(rowsToDraw()[index]?.date) : "";
          },
          label(context) {
            const label = context.dataset.label || "";
            return view === "ratio"
              ? `${label}: ${Number(context.raw).toFixed(2)}%`
              : `${label}: ${Number(context.raw).toFixed(2)}조원`;
          },
        },
      },
    },
    elements: {
      line: { tension: 0.22, borderWidth: 2.35 },
      point: { radius: rowsToDraw().length === 1 ? 6 : 0, hoverRadius: 6, hitRadius: 14 },
    },
  };

  if (view === "ratio") {
    options.scales = {
      x: {
        grid: { display: false },
        border: { display: false },
        ticks: { color: css("--muted"), maxTicksLimit: 9, maxRotation: 0 },
      },
      y: {
        beginAtZero: false,
        border: { display: false },
        grid: { color: "rgba(255,255,255,.065)" },
        ticks: { color: css("--muted"), callback: (value) => `${value}%` },
      },
    };
  } else {
    options.scales = {
      x: {
        grid: { display: false },
        border: { display: false },
        ticks: { color: css("--muted"), maxTicksLimit: 9, maxRotation: 0 },
      },
      yCredit: {
        type: "linear",
        position: "left",
        beginAtZero: false,
        border: { display: false },
        grid: { color: "rgba(255,255,255,.065)" },
        ticks: { color: css("--credit"), callback: (value) => `${value}조` },
        title: { display: true, text: "신용융자", color: css("--credit") },
      },
      yDeposit: {
        type: "linear",
        position: "right",
        beginAtZero: false,
        border: { display: false },
        grid: { drawOnChartArea: false },
        ticks: { color: css("--deposit"), callback: (value) => `${value}조` },
        title: { display: true, text: "고객예탁금", color: css("--deposit") },
      },
    };
  }
  return options;
}

function rowsToDraw() {
  return sixMonthRows(rows);
}

function chartConfig(view) {
  const drawRows = rowsToDraw();
  const includeYear = drawRows.length > 1 && drawRows[0].date.slice(0, 4) !== drawRows.at(-1).date.slice(0, 4);
  const labels = drawRows.map((row) => shortDate(row.date, includeYear));

  if (view === "ratio") {
    return {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "예탁금 대비 신용 비율",
          data: drawRows.map((row) => row.ratio),
          borderColor: css("--accent"),
          backgroundColor: "rgba(74, 132, 255, .14)",
          fill: true,
        }],
      },
      options: commonOptions(view),
    };
  }

  return {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "신용융자 잔고",
          data: drawRows.map((row) => row.credit_trillion),
          borderColor: css("--credit"),
          backgroundColor: "rgba(255, 106, 128, .08)",
          yAxisID: "yCredit",
        },
        {
          label: "고객예탁금",
          data: drawRows.map((row) => row.deposit_trillion),
          borderColor: css("--deposit"),
          backgroundColor: "rgba(87, 220, 184, .08)",
          yAxisID: "yDeposit",
        },
      ],
    },
    options: commonOptions(view),
  };
}

function renderChart() {
  chart?.destroy();
  chart = null;

  const canvas = $("trendChart");
  const empty = $("emptyState");
  if (rows.length < 1 || typeof Chart === "undefined") {
    canvas.classList.add("hidden");
    empty.classList.remove("hidden");
    if (typeof Chart === "undefined") {
      empty.querySelector("strong").textContent = "차트 라이브러리를 불러오지 못했습니다.";
      empty.querySelector("span").textContent = "페이지를 새로고침해 주세요.";
    }
    return;
  }

  canvas.classList.remove("hidden");
  empty.classList.add("hidden");
  chart = new Chart(canvas, chartConfig(currentView));
  if (rows.length === 1) {
    $("chartCaption").textContent = "첫 자동 수집값입니다. 다음 영업일 데이터부터 날짜별 추세선이 이어집니다.";
  } else {
    $("chartCaption").textContent = currentView === "ratio"
      ? "최근 6개월 · 신용융자 잔고 ÷ 고객예탁금 × 100으로 다시 계산한 비율입니다."
      : "최근 6개월 · 두 지표는 좌·우 축을 각각 사용합니다. 높이보다 방향과 변화 속도를 비교하세요.";
  }
}

function setTab(view) {
  currentView = view;
  document.querySelectorAll(".tab").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  renderChart();
}

function renderLatest(latest) {
  if (!latest) return;
  const ratio = latest.credit_trillion / latest.deposit_trillion * 100;
  $("heroRatio").textContent = fmtPercent(ratio, 1);
  $("asOfText").textContent = `${fmtDate(latest.date)} 기준 · 고객예탁금은 공시 시차가 있을 수 있습니다.`;
  $("latestCredit").textContent = fmtTrillion(latest.credit_trillion);
  $("latestDeposit").textContent = fmtTrillion(latest.deposit_trillion);

  const state = ratioState(ratio);
  const stateElement = $("ratioState");
  stateElement.textContent = state.label;
  stateElement.className = `state-pill ${state.className}`;

  const previous = rows.length >= 2 ? rows.at(-2).ratio : NaN;
  setChange(ratio, previous);
}

async function init() {
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => setTab(button.dataset.view));
  });

  try {
    const response = await fetch(DATA_URL, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    dataset = await response.json();
    rows = normalizeRows(dataset.series);
    const latestFromFile = normalizeLatest(dataset.latest);

    // data.json에 최신 요약값만 있고 series가 비어 있어도 차트를 표시한다.
    // series가 오래된 경우에는 같은 날짜를 교체하거나 최신 날짜를 뒤에 추가한다.
    if (latestFromFile) {
      const sameDateIndex = rows.findIndex((row) => row.date === latestFromFile.date);
      if (sameDateIndex >= 0) {
        rows[sameDateIndex] = latestFromFile;
      } else {
        rows.push(latestFromFile);
      }
      rows.sort((a, b) => a.date.localeCompare(b.date));
    }
    rows = sixMonthRows(rows);

    const latest = rows.at(-1) || latestFromFile;

    renderLatest(latest);
    if (!latest) {
      $("asOfText").textContent = "정확한 최신 데이터를 확인하지 못했습니다.";
      $("ratioChange").textContent = "데이터 없음";
    }

    $("updatedAt").textContent = dataset.updated_at
      ? `마지막 자동 수집: ${new Date(dataset.updated_at).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })}`
      : "자동 수집 전 임시 최신값 표시 중";

    renderChart();
  } catch (error) {
    console.error(error);
    $("trendChart").classList.add("hidden");
    $("emptyState").classList.remove("hidden");
    $("emptyState").querySelector("strong").textContent = "데이터를 불러오지 못했습니다.";
    $("emptyState").querySelector("span").textContent = "data.json과 GitHub Actions 실행 기록을 확인해 주세요.";
  }
}

window.addEventListener("load", init);
