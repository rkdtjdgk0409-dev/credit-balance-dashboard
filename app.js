const DATA_URL = `data.json?v=${Date.now()}`;
let chart;
let currentView = "ratio";
let dataset = null;

const $ = (id) => document.getElementById(id);

function fmtDate(value) {
  if (!value) return "—";
  const d = new Date(`${value}T00:00:00`);
  if (Number.isNaN(d.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", { year: "numeric", month: "long", day: "numeric" }).format(d);
}

function fmtTrillion(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n.toLocaleString("ko-KR", { minimumFractionDigits: 1, maximumFractionDigits: 2 })}조원`;
}

function fmtPercent(value, digits = 1) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—%";
  return `${n.toLocaleString("ko-KR", { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
}

function shortDate(value) {
  const d = new Date(`${value}T00:00:00`);
  if (Number.isNaN(d.getTime())) return value;
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function setChange(latest, previous) {
  const el = $("ratioChange");
  if (!Number.isFinite(latest) || !Number.isFinite(previous)) {
    el.textContent = "변화율 없음";
    el.className = "change-pill neutral";
    return;
  }
  const delta = latest - previous;
  if (Math.abs(delta) < 0.005) {
    el.textContent = "전일과 동일";
    el.className = "change-pill neutral";
  } else {
    el.textContent = `${delta > 0 ? "▲" : "▼"} ${Math.abs(delta).toFixed(2)}%p`;
    el.className = `change-pill ${delta > 0 ? "up" : "down"}`;
  }
}

function chartConfig(view, rows) {
  const common = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    animation: { duration: 450 },
    plugins: {
      legend: {
        display: view === "compare",
        position: "top",
        align: "start",
        labels: { color: css("--muted"), usePointStyle: true, boxWidth: 8, boxHeight: 8, padding: 16 }
      },
      tooltip: {
        backgroundColor: "rgba(10,12,16,.96)",
        borderColor: "rgba(255,255,255,.12)",
        borderWidth: 1,
        titleColor: css("--text"),
        bodyColor: css("--text"),
        padding: 12,
        callbacks: {
          label(ctx) {
            const label = ctx.dataset.label || "";
            return view === "ratio"
              ? `${label}: ${ctx.parsed.y.toFixed(2)}%`
              : `${label}: ${ctx.parsed.y.toFixed(2)}조원`;
          }
        }
      }
    },
    scales: {
      x: {
        grid: { display: false },
        border: { display: false },
        ticks: { color: css("--muted"), maxTicksLimit: 9, maxRotation: 0, autoSkip: true }
      },
      y: {
        beginAtZero: false,
        border: { display: false },
        grid: { color: "rgba(255,255,255,.065)" },
        ticks: {
          color: css("--muted"),
          callback: (v) => view === "ratio" ? `${v}%` : `${v}조`
        }
      }
    },
    elements: {
      line: { tension: 0.26, borderWidth: 2.2 },
      point: { radius: 0, hoverRadius: 4, hitRadius: 14 }
    }
  };

  if (view === "ratio") {
    return {
      type: "line",
      data: {
        labels: rows.map((r) => shortDate(r.date)),
        datasets: [{
          label: "예탁금 대비 신용 비율",
          data: rows.map((r) => r.ratio),
          borderColor: css("--accent"),
          backgroundColor: "rgba(124,156,255,.12)",
          fill: true
        }]
      },
      options: common
    };
  }

  return {
    type: "line",
    data: {
      labels: rows.map((r) => shortDate(r.date)),
      datasets: [
        {
          label: "신용융자 잔고",
          data: rows.map((r) => r.credit_trillion),
          borderColor: css("--danger"),
          backgroundColor: "rgba(255,122,140,.06)"
        },
        {
          label: "고객예탁금",
          data: rows.map((r) => r.deposit_trillion),
          borderColor: css("--accent-2"),
          backgroundColor: "rgba(126,224,195,.06)"
        }
      ]
    },
    options: common
  };
}

function renderChart() {
  if (!dataset?.series?.length || typeof Chart === "undefined") return;
  const rows = dataset.series.slice(-180);
  chart?.destroy();
  chart = new Chart($("trendChart"), chartConfig(currentView, rows));
  $("chartCaption").textContent = currentView === "ratio"
    ? "신용융자 잔고를 고객예탁금으로 나눈 비율입니다. 최근 최대 180개 관측치를 표시합니다."
    : "신용융자 잔고와 고객예탁금을 같은 조원 단위로 비교합니다. 최근 최대 180개 관측치를 표시합니다.";
}

function setTab(view) {
  currentView = view;
  for (const button of document.querySelectorAll(".tab")) {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  }
  renderChart();
}

async function init() {
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => setTab(button.dataset.view));
  });

  try {
    const response = await fetch(DATA_URL, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    dataset = await response.json();
    const rows = Array.isArray(dataset.series) ? dataset.series : [];
    if (!rows.length) {
      $("trendChart").classList.add("hidden");
      $("emptyState").classList.remove("hidden");
      $("updatedAt").textContent = dataset.message || "첫 데이터 수집 전입니다.";
      return;
    }

    const latest = rows.at(-1);
    const prev = rows.at(-2);
    $("heroRatio").textContent = fmtPercent(latest.ratio, 1);
    $("asOfText").textContent = `${fmtDate(latest.date)} 기준`;
    $("latestCredit").textContent = fmtTrillion(latest.credit_trillion);
    $("latestDeposit").textContent = fmtTrillion(latest.deposit_trillion);
    $("updatedAt").textContent = dataset.updated_at
      ? `마지막 수집: ${new Date(dataset.updated_at).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })}`
      : "업데이트 시각 없음";
    setChange(Number(latest.ratio), Number(prev?.ratio));
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
