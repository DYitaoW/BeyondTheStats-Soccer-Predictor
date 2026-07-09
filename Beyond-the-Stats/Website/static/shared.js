const panelHome = document.getElementById("panel-home");
const panelGlobal = document.getElementById("panel-global");
const panelCups = document.getElementById("panel-cups");
const panelLeagueTable = document.getElementById("panel-league-table");
const panelWorldCup = document.getElementById("panel-world-cup");
const panelPlayers = document.getElementById("panel-players");
const panelAbout = document.getElementById("panel-about");
const tabHome = document.getElementById("tab-home");
const tabGlobal = document.getElementById("tab-global");
const tabCups = document.getElementById("tab-cups");
const tabH2H = document.getElementById("tab-h2h");
const tabLeagueTable = document.getElementById("tab-league-table");
const tabWorldCup = document.getElementById("tab-world-cup");
const tabPlayers = document.getElementById("tab-players");
const tabTactics = document.getElementById("tab-tactics");
const headerAbout = document.getElementById("header-about");
const globalList = document.getElementById("global-list");
const globalStats = document.getElementById("global-stats");
const globalSourceFilter = document.getElementById("global-source-filter");
const globalLeagueFilter = document.getElementById("global-league-filter");
const globalLeagueFilterCard = document.getElementById("global-league-filter-card");
const cupTabs = document.getElementById("cup-tabs");
const tableDataset = document.getElementById("table-dataset");
const tableLeague = document.getElementById("table-league");
const tableViewToggle = document.getElementById("table-view-toggle");
const tablePositionOddsToggle = document.getElementById("table-position-odds-toggle");
const leagueTableView = document.getElementById("league-table-view");
const winnerDataset = document.getElementById("winner-dataset");
const winnerView = document.getElementById("winner-view");
const panelH2H = document.getElementById("panel-h2h");
const h2hResults = document.getElementById("h2h-results");
const h2hDataset = document.getElementById("h2h-dataset");
const h2hTeam1Input = document.getElementById("h2h-team1");
const h2hTeam2Input = document.getElementById("h2h-team2");
const topPicksList = document.getElementById("top-picks-list");
const feedbackText = document.getElementById("feedback-text");
const feedbackSubmit = document.getElementById("feedback-submit");
const brandHomeBtn = document.getElementById("brand-home-btn");
const cupProjectionTabs = document.getElementById("cup-projection-tabs");
const cupViewTable = document.getElementById("cup-view-table");
const cupViewBracket = document.getElementById("cup-view-bracket");
const cupFormatNote = document.getElementById("cup-format-note");
const cupTableView = document.getElementById("cup-table-view");
const cupBracketView = document.getElementById("cup-bracket-view");
const leagueTablesCache = { global: null, mls: null, extra: null, cups: null };
let tableViewMode = "standings";
let tablePositionOddsMode = false;
let activeCupProjectionCompetition = "UEFA/Champions League";
let activeCupProjectionView = "table";
const upcomingCache = { global: [], mls: [], extra: [], cups: [], friendlies: [] };
const upcomingStatsCache = {
global: { stats: null, league_stats: [] },
mls: { stats: null, league_stats: [] },
extra: { stats: null, league_stats: [] },
cups: { stats: null, league_stats: [] },
friendlies: { stats: null, league_stats: [] },
};
const FRIENDLIES_OPTIONS = ["Club Friendlies"];
const cupPredictionTabs = [
{ key: "all", label: "All Cups", competitions: [] },
{ key: "fa-cup", label: "FA Cup", competitions: ["England/FA Cup"] },
{ key: "league-cup", label: "League Cup", competitions: ["England/League Cup"] },
{ key: "leagues-cup", label: "Leagues Cup", competitions: ["CONCACAF/Leagues Cup"] },
{ key: "champions-league", label: "Champions League", competitions: ["UEFA/Champions League", "Europe/Champions League"] },
{ key: "europa-league", label: "Europa League", competitions: ["UEFA/Europa League", "Europe/Europa League"] },
{ key: "conference-league", label: "Conference League", competitions: ["UEFA/Conference League", "Europe/Conference League"] },
{ key: "dfb-pokal", label: "DFB-Pokal", competitions: ["Germany/DFB-Pokal"] },
{ key: "coppa-italia", label: "Coppa Italia", competitions: ["Italy/Coppa Italia"] },
{ key: "copa-del-rey", label: "Copa del Rey", competitions: ["Spain/Copa del Rey"] },
{ key: "coupe-de-france", label: "Coupe de France", competitions: ["France/Coupe de France"] },
];
const cupProjectionConfigs = [
{ key: "ucl", label: "Champions League", competition: "UEFA/Champions League", aliases: ["UEFA/Champions League", "Europe/Champions League"], hasTable: true, leaguePhaseMatches: 8 },
{ key: "uel", label: "Europa League", competition: "UEFA/Europa League", aliases: ["UEFA/Europa League", "Europe/Europa League"], hasTable: true, leaguePhaseMatches: 8 },
{ key: "uecl", label: "Conference League", competition: "UEFA/Conference League", aliases: ["UEFA/Conference League", "Europe/Conference League"], hasTable: true, leaguePhaseMatches: 6 },
{ key: "fa-cup", label: "FA Cup", competition: "England/FA Cup", aliases: ["England/FA Cup"], hasTable: false, leaguePhaseMatches: null },
{ key: "league-cup", label: "League Cup", competition: "England/League Cup", aliases: ["England/League Cup"], hasTable: false, leaguePhaseMatches: null },
{ key: "leagues-cup", label: "Leagues Cup", competition: "CONCACAF/Leagues Cup", aliases: ["CONCACAF/Leagues Cup"], hasTable: true, leaguePhaseMatches: 3 },
{ key: "dfb-pokal", label: "DFB-Pokal", competition: "Germany/DFB-Pokal", aliases: ["Germany/DFB-Pokal"], hasTable: false, leaguePhaseMatches: null },
{ key: "coppa-italia", label: "Coppa Italia", competition: "Italy/Coppa Italia", aliases: ["Italy/Coppa Italia"], hasTable: false, leaguePhaseMatches: null },
{ key: "copa-del-rey", label: "Copa del Rey", competition: "Spain/Copa del Rey", aliases: ["Spain/Copa del Rey"], hasTable: false, leaguePhaseMatches: null },
{ key: "coupe-de-france", label: "Coupe de France", competition: "France/Coupe de France", aliases: ["France/Coupe de France"], hasTable: false, leaguePhaseMatches: null },
];
let activeCupTab = "all";
const mlsTeamSet = new Set(
    Array.from(document.querySelectorAll("#mls-teams option"))
    .map((opt) => String(opt.value || "").trim().toLowerCase())
    .filter(Boolean)
);
const extraTeamSet = new Set(
    Array.from(document.querySelectorAll("#extra-teams option"))
    .map((opt) => String(opt.value || "").trim().toLowerCase())
    .filter(Boolean)
);

// Hardcoded league lists: always shown in dropdowns, regardless of which
// leagues currently have data. Order matches the priority in the dropdown.
const EUROPEAN_LEAGUES = [
    "England/Premier League",
    "England/Championship",
    "Spain/La Liga",
    "Spain/La Liga 2",
    "Germany/Bundesliga",
    "Germany/Bundesliga 2",
    "Italy/Serie A",
    "Italy/Serie B",
    "France/Ligue 1",
    "France/Ligue 2",
    "Belgium/First Division A",
    "Netherlands/Eredivisie",
    "Portugal/Liga Portugal",
    "Scotland/Premiership",
    "Turkey/Super Lig",
    "Austria/Bundesliga",
    "Greece/Super League",
    "Norway/Eliteserien",
    "Romania/Liga I",
    "Poland/Ekstraklasa",
    "Sweden/Allsvenskan",
];
const EUROPEAN_CUPS = [
    "UEFA/Champions League",
    "UEFA/Europa League",
    "UEFA/Conference League",
    "England/FA Cup",
    "England/League Cup",
    "CONCACAF/Leagues Cup",
    "Germany/DFB-Pokal",
    "Italy/Coppa Italia",
    "Spain/Copa del Rey",
    "France/Coupe de France",
];
const MLS_LEAGUES = [
    "United States/MLS",
    "United States/MLS - Supporters Shield Table",
    "United States/MLS - Eastern Conference",
    "United States/MLS - Western Conference",
    "Mexico/Liga MX",
];
const OTHER_LEAGUES = [
    "Argentina/Primera Division",
    "Brazil/Serie A",
    "Japan/J1 League",
];
const FRIENDLIES_LEAGUES = ["Club Friendlies"];
const WORLD_CUP_OPTIONS = ["FIFA/World Cup"];

function getLeaguesForSource(source) {
    if (source === "mls") return [...MLS_LEAGUES];
    if (source === "extra") return [...OTHER_LEAGUES];
    if (source === "cups") return [...EUROPEAN_CUPS];
    if (source === "world-cup") return [...WORLD_CUP_OPTIONS];
    if (source === "friendlies") return [...FRIENDLIES_OPTIONS];
    return [...EUROPEAN_LEAGUES, ...EUROPEAN_CUPS, ...MLS_LEAGUES, ...OTHER_LEAGUES, ...FRIENDLIES_LEAGUES];
}

function getLeaguesForDataset(dataset) {
    if (dataset === "mls") return [...MLS_LEAGUES];
    if (dataset === "extra") return [...OTHER_LEAGUES];
    if (dataset === "cups") return [...EUROPEAN_CUPS];
    if (dataset === "world-cup") return [...WORLD_CUP_OPTIONS];
    return [...EUROPEAN_LEAGUES, ...EUROPEAN_CUPS];
}

// Dark Mode Logic
const themeToggle = document.getElementById("theme-toggle");
if (themeToggle) {
themeToggle.addEventListener("click", () => {
document.body.classList.toggle("dark-mode");
const isDark = document.body.classList.contains("dark-mode");
themeToggle.textContent = isDark ? "Light Mode" : "Dark Mode";
localStorage.setItem("theme", isDark ? "dark" : "light");
});
}
if (localStorage.getItem("theme") === "dark") {
document.body.classList.add("dark-mode");
if (themeToggle) {
    themeToggle.textContent = "Light Mode";
}
}

function showNotification(message) {
const area = document.getElementById('notification-area');
const el = document.createElement('div');
el.className = 'notification';
el.textContent = message;
area.appendChild(el);
setTimeout(() => {
    el.style.animation = 'fadeOut 0.3s forwards';
    setTimeout(() => el.remove(), 300);
}, 3000);
}

function showError(targetError, targetResult, message) {
targetError.textContent = message;
targetError.classList.remove("hidden");
targetResult.classList.add("hidden");
}

async function submitFeedback() {
const message = String(feedbackText?.value || "").trim();
if (!message) {
    showNotification("Please enter feedback before sending.");
    return;
}
feedbackSubmit.disabled = true;
try {
    const resp = await fetch("/api/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ feedback: message }),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
    throw new Error(data.error || "Failed to submit feedback.");
    }
    feedbackText.value = "";
    showNotification("Feedback sent! Thank you.");
} catch (err) {
    showNotification(`Feedback failed: ${err.message}`);
} finally {
    feedbackSubmit.disabled = false;
}
}

function formatPercent(value, withSymbol = true) {
const n = Number(value);
if (!Number.isFinite(n) || n <= 0) return withSymbol ? "0%" : "0";
if (n < 1) return withSymbol ? "<1%" : "<1";
return `${n.toFixed(1)}${withSymbol ? "%" : ""}`;
}

function pctLabel(value) {
return formatPercent(value, false);
}

function showResult(targetError, targetResult, p, includeShots = true) {
targetError.classList.add("hidden");
targetResult.classList.remove("hidden");
const topOutcome = Math.max(Number(p.prob_home) || 0, Number(p.prob_draw) || 0, Number(p.prob_away) || 0);
let html = `
    <div class="result-head">
    <h2>${p.home_team} vs ${p.away_team}</h2>
    <span class="confidence-pill">${pctLabel(topOutcome)}% confidence</span>
    </div>
    <p class="match-meta"><strong>Competition:</strong> ${p.competition}</p>
    <p class="match-meta">Winner: <span class="winner-line">${p.winner_label}</span></p>
    <div class="probability-wrap">
        <div class="probability-labels">
            <span>${p.home_team} (${pctLabel(p.prob_home)}%)</span>
            <span>Draw (${pctLabel(p.prob_draw)}%)</span>
            <span>${p.away_team} (${pctLabel(p.prob_away)}%)</span>
        </div>
        <div class="probability-track">
            <div style="width: ${p.prob_home}%;" title="${p.home_team}"></div>
            <div style="width: ${p.prob_draw}%;" title="Draw"></div>
            <div style="width: ${p.prob_away}%;" title="${p.away_team}"></div>
        </div>
    </div>

    <p class="match-meta"><strong>Predicted score:</strong> ${p.home_team} ${p.pred_home_goals} - ${p.pred_away_goals} ${p.away_team}</p>
`;
if (includeShots) {
    html += `
    <p class="match-meta"><strong>Predicted shots:</strong> ${p.home_team} ${p.pred_home_shots} | ${p.away_team} ${p.pred_away_shots}</p>
    <p class="match-meta"><strong>Predicted shots on target:</strong> ${p.home_team} ${p.pred_home_sot} | ${p.away_team} ${p.pred_away_sot}</p>
    `;
}
targetResult.innerHTML = html;
}

function activateTab(tab) {
// safely reset all tabs/panels so per-page templates without every section do not throw
    [tabHome, tabGlobal, tabCups, tabH2H, tabLeagueTable, tabWorldCup, tabPlayers, headerAbout]
    .filter(Boolean)
    .forEach((node) => node.classList.remove("active"));
    [panelHome, panelGlobal, panelCups, panelH2H, panelLeagueTable, panelWorldCup, panelPlayers, panelAbout]
    .filter(Boolean)
    .forEach((node) => node.classList.add("hidden"));

if (tab === "home") {
    tabHome.classList.add("active");
    panelHome.classList.remove("hidden");
} else if (tab === "global") {
    tabGlobal.classList.add("active");
    panelGlobal.classList.remove("hidden");
} else if (tab === "cups") {
    tabCups.classList.add("active");
    panelCups.classList.remove("hidden");
} else if (tab === "h2h") {
    tabH2H.classList.add("active");
    panelH2H.classList.remove("hidden");
} else if (tab === "league-table") {
    tabLeagueTable.classList.add("active");
    panelLeagueTable.classList.remove("hidden");
} else if (tab === "world-cup") {
    tabWorldCup.classList.add("active");
    panelWorldCup.classList.remove("hidden");
} else if (tab === "players") {
    tabPlayers.classList.add("active");
    panelPlayers.classList.remove("hidden");
} else if (tab === "about") {
    if (headerAbout) headerAbout.classList.add("active");
    panelAbout.classList.remove("hidden");
} else {
    tabHome.classList.add("active");
    panelHome.classList.remove("hidden");
}
}

function normalizeLeagueName(name) {
return String(name || "").toLowerCase().replace(/\s+/g, " ").trim();
}

function getLeagueRowClass(leagueName, position, maxPos) {
const league = normalizeLeagueName(leagueName);
const isMlsSupporters = league === "united states/mls - supporters shield table";
const isMlsEast = league === "united states/mls - eastern conference";
const isMlsWest = league === "united states/mls - western conference";
const isUefaCupTable = [
    "uefa/champions league",
    "europe/champions league",
    "uefa/europa league",
    "europe/europa league",
    "uefa/conference league",
    "europe/conference league"
].includes(league);

if (isMlsEast || isMlsWest) {
    if (position >= 1 && position <= 9) {
    return "table-promo-blue";
    }
    return "";
}

if (isUefaCupTable) {
    if (position >= 1 && position <= 8) {
    return "table-first";
    }
    if (position >= 9 && position <= 24) {
    return "table-promo-blue";
    }
    return "";
}

if (position === 1) {
    return "table-first";
}

const isBundesliga = league === "germany/bundesliga";
const isLigue1 = league === "france/ligue 1";
const isChampionship = league === "england/championship";
const isLaLiga2 = league === "spain/la liga 2";
const isSerieB = league === "italy/serie b";
const isBundesliga2 = league === "germany/bundesliga 2" || league === "germany/2. bundesliga";
const isLigue2 = league === "france/ligue 2";
const isLigaPortugal = league === "portugal/liga portugal";
const isPremier = league === "england/premier league";
const isLaLiga = league === "spain/la liga";
const isBundesligaTop = league === "germany/bundesliga";
const isSerieA = league === "italy/serie a";

if (isBundesliga || isLigue1) {
    if (position === 2 || position === 3) {
    return "table-promo-blue";
    }
    if (position === 4) {
    return "table-playoff-purple";
    }
    if (position === Math.max(1, maxPos - 2)) {
    return "table-playoff-orange";
    }
    if (position >= Math.max(1, maxPos - 1)) {
    return "table-bottom";
    }
    return "";
}

if (isChampionship) {
    if (position === 2) {
    return "table-promo-blue";
    }
    if (position >= 3 && position <= 6) {
    return "table-playoff-purple";
    }
}

if (isSerieB) {
    if (position === 2) {
    return "table-promo-blue";
    }
    if (position >= 3 && position <= 8) {
    return "table-playoff-purple";
    }
}

if (isLaLiga2) {
    if (position === 2) {
    return "table-promo-blue";
    }
    if (position >= 3 && position <= 6) {
    return "table-playoff-purple";
    }
}

if (isBundesliga2 || isLigue2) {
    if (position === 2) {
    return "table-promo-blue";
    }
    if (isLigue2 && position >= 3 && position <= 5) {
    return "table-playoff-purple";
    }
    if (!isLigue2 && position === 3) {
    return "table-playoff-purple";
    }
}

if (isLigaPortugal) {
    if (position === 2) {
    return "table-second-pink";
    }
    if (position === Math.max(1, maxPos - 2)) {
    return "table-playoff-orange";
    }
    if (position >= Math.max(1, maxPos - 1)) {
    return "table-bottom";
    }
}

if (isPremier || isLaLiga || isSerieA) {
    if (position >= 2 && position <= 4) {
    return "table-promo-blue";
    }
    if (position === 5) {
    return "table-playoff-purple";
    }
}

if (isMlsSupporters) {
    return "";
}

if (isLaLiga2) {
    if (position >= Math.max(1, maxPos - 3)) {
    return "table-bottom";
    }
} else if (position >= Math.max(1, maxPos - 2)) {
    return "table-bottom";
}

return "";
}

function renderLeagueTableRows(rows, leagueName) {
if (!rows || !rows.length) {
    return "<p>No projected table data available for this league.</p>";
}
const sortedRows = [...rows].sort((a, b) => (a.position || 0) - (b.position || 0));
const maxPos = sortedRows.length;
let html = `
    <table class="league-table">
    <thead>
        <tr>
        <th>Pos</th><th>Team</th><th>P</th><th>W</th><th>D</th><th>L</th>
        <th>GF</th><th>GA</th><th>GD</th><th>Pts</th>
        </tr>
    </thead>
    <tbody>
`;
for (const r of sortedRows) {
    const rowClass = getLeagueRowClass(leagueName, r.position, maxPos);
    html += `
    <tr class="${rowClass}">
        <td>${r.position}</td><td>${r.team}</td><td>${r.P}</td><td>${r.W}</td><td>${r.D}</td><td>${r.L}</td>
        <td>${r.GF}</td><td>${r.GA}</td><td>${r.GD}</td><td><strong>${r.Pts}</strong></td>
    </tr>
    `;
}
html += "</tbody></table>";
return html;
}

function asPct(value) {
return formatPercent(value, true);
}

function asWholePct(value) {
if (value === null || value === undefined || value === "") return "—";
const n = Number(value);
if (!Number.isFinite(n) || n <= 0) return "0%";
if (n < 1) return "<1%";
return `${Math.round(n)}%`;
}

function renderLeagueProbabilityRows(rows) {
if (!rows || !rows.length) {
    return "<p>No probability data available for this league.</p>";
}
const sortedRows = [...rows].sort((a, b) => {
    const aw = Number(a.win_league_pct) || 0;
    const bw = Number(b.win_league_pct) || 0;
    if (bw !== aw) return bw - aw;
    return (Number(a.position) || 999) - (Number(b.position) || 999);
});
let html = `
    <table class="league-table">
    <thead>
        <tr>
        <th>Team</th><th>Win League</th><th>Top 4</th><th>Bottom 3</th><th>Most Likely Finish</th>
        </tr>
    </thead>
    <tbody>
`;
for (const r of sortedRows) {
    const likelyPos = Number(r.most_likely_position);
    const likelyPosText = Number.isFinite(likelyPos) ? `#${likelyPos}` : "N/A";
    html += `
    <tr>
        <td>${r.team}</td>
        <td>${asPct(r.win_league_pct)}</td>
        <td>${asPct(r.top4_pct)}</td>
        <td>${asPct(r.bottom3_pct)}</td>
        <td>${likelyPosText} (${asPct(r.most_likely_position_pct)})</td>
    </tr>
    `;
}
html += "</tbody></table>";
return html;
}

function updateTableViewToggleLabel() {
if (!tableViewToggle) {
    return;
}
tableViewToggle.textContent = tableViewMode === "standings"
    ? "Show Probability View"
    : "Show Standings View";
}

function renderSelectedLeagueTable() {
const mode = tableDataset.value;
const selectedLeague = tableLeague.value;
const payload = leagueTablesCache[mode];
if (mode === "cups" && selectedLeague.startsWith("__cup_bracket__:")) {
    renderCupBracket(payload, selectedLeague.replace("__cup_bracket__:", ""));
    return;
}
if (!payload || !payload.tables || !payload.tables[selectedLeague]) {
    leagueTableView.innerHTML = "<p>No projected table data available.</p>";
    return;
}
const rows = payload.tables[selectedLeague];
// Position-odds view wins over the simple probability view when both are active,
// since it surfaces the same data (chance of finishing 1st / 2nd / ...) in a
// more detailed grid per team.
if (tablePositionOddsMode) {
    leagueTableView.innerHTML = `<h3>${selectedLeague}</h3>${renderPositionOddsRows(rows, selectedLeague)}`;
} else if (tableViewMode === "probability") {
    leagueTableView.innerHTML = `<h3>${selectedLeague}</h3>${renderLeagueProbabilityRows(rows)}`;
} else {
    leagueTableView.innerHTML = `<h3>${selectedLeague}</h3>${renderLeagueTableRows(rows, selectedLeague)}`;
}
}

function updateTablePositionOddsToggleLabel() {
if (!tablePositionOddsToggle) {
    return;
}
tablePositionOddsToggle.textContent = tablePositionOddsMode
    ? "Hide Position Odds"
    : "Show Position Odds";
}

function renderPositionOddsRows(rows, leagueName) {
if (!rows || !rows.length) {
    return "<p>No position odds data available for this league.</p>";
}
const sortedRows = [...rows].sort((a, b) => (a.position || 0) - (b.position || 0));
const totalPositions = sortedRows.length;
const headers = Array.from({ length: totalPositions }, (_, idx) => `<th>#${idx + 1}</th>`).join("");
let html = `
    <table class="league-table">
    <thead>
        <tr>
        <th>Team</th>${headers}
        </tr>
    </thead>
    <tbody>
`;
for (const row of sortedRows) {
    const mostLikelyPos = Number(row.most_likely_position);
    const odds = row.position_odds;
    const hasOdds = odds && Object.keys(odds).length > 0;
    let cells = "";
    for (let pos = 1; pos <= totalPositions; pos += 1) {
    const raw = hasOdds ? (odds[pos] !== undefined ? odds[pos] : odds[String(pos)]) : null;
    const highlightClass = pos === mostLikelyPos ? "position-odds-best" : "";
    cells += `<td class="${highlightClass}">${asWholePct(raw)}</td>`;
    }
    html += `
    <tr class="${getLeagueRowClass(leagueName, row.position, totalPositions)}">
        <td>${row.team}</td>${cells}
    </tr>
    `;
}
html += "</tbody></table>";
return html;
}

function renderWinnerView() {
// guard: this shared script is loaded on pages that may not include the winner module
if (!winnerDataset || !winnerView) {
    return;
}
const mode = winnerDataset.value;
const payload = leagueTablesCache[mode];
if (!payload || !payload.tables) {
    winnerView.innerHTML = "<p>No winner data available.</p>";
    return;
}
const leagues = Object.keys(payload.tables || {}).filter((name) => name !== "__mls_bracket__");
if (!leagues.length) {
    winnerView.innerHTML = "<p>No winner data available.</p>";
    return;
}
const ordered = [...leagues].sort((a, b) => a.localeCompare(b));
let html = `
    <table class="league-table">
    <thead>
        <tr>
        <th>League</th><th>Predicted Winner</th><th>Win Chance</th>
        </tr>
    </thead>
    <tbody>
`;
for (const league of ordered) {
    const rows = payload.tables[league] || [];
    if (!rows.length) continue;
    const winner = [...rows].sort((a, b) => {
    const aw = Number(a.win_league_pct) || 0;
    const bw = Number(b.win_league_pct) || 0;
    if (bw !== aw) return bw - aw;
    return (Number(a.position) || 999) - (Number(b.position) || 999);
    })[0];
    html += `
    <tr>
        <td>${league}</td>
        <td>${winner ? winner.team : "N/A"}</td>
        <td>${winner ? asPct(winner.win_league_pct) : "0%"}</td>
    </tr>
    `;
}
html += "</tbody></table>";
winnerView.innerHTML = html;
}

function setLeagueSelectOptions(selectEl, leagues, includeMlsBracket = false, cupBrackets = null, dataset = "global") {
selectEl.innerHTML = "";
// Use the dataset to pick the hardcoded per-source list,
// so all supported leagues are always in the dropdown regardless of data load.
const hardcodedLeagues = getLeaguesForDataset(dataset);
const leagueRank = (name) => {
    const priorityLeagues = [
        "England/Premier League",
        "England/Championship",
        "United States/MLS - Supporters Shield Table",
        "United States/MLS - Eastern Conference",
        "United States/MLS - Western Conference"
    ];
    const idx = priorityLeagues.indexOf(name);
    return idx >= 0 ? idx : 1000;
};
const orderedLeagues = [...hardcodedLeagues].sort((a, b) => {
    const ra = leagueRank(a);
    const rb = leagueRank(b);
    if (ra !== rb) return ra - rb;
    return a.localeCompare(b);
});
for (const league of orderedLeagues) {
    const option = document.createElement("option");
    option.value = league;
    option.textContent = league;
    selectEl.appendChild(option);
}
if (includeMlsBracket) {
    const option = document.createElement("option");
    option.value = "__mls_bracket__";
    option.textContent = "MLS Cup Playoff Bracket";
    selectEl.appendChild(option);
}
const cupCompetitions = cupBrackets && cupBrackets.competitions ? Object.keys(cupBrackets.competitions) : [];
for (const competition of cupCompetitions.sort((a, b) => a.localeCompare(b))) {
    const option = document.createElement("option");
    option.value = `__cup_bracket__:${competition}`;
    option.textContent = `${competition} Fixture Bracket`;
    selectEl.appendChild(option);
}
}

function seedAt(rows, seed) {
const row = (rows || []).find((r) => Number(r.position) === Number(seed));
return row ? row.team : `Seed ${seed}`;
}

function renderMlsBracket(payload) {
const bracket = payload ? payload.bracket : null;
if (!bracket) {
    leagueTableView.innerHTML = "<p>No projected MLS playoff bracket found. Run MLS Project_League_Table.py first.</p>";
    return;
}

const eastSeeds = bracket.eastern_seeds || [];
const westSeeds = bracket.western_seeds || [];
const eastBySeed = {};
const westBySeed = {};
for (const row of eastSeeds) eastBySeed[Number(row.seed)] = row.team;
for (const row of westSeeds) westBySeed[Number(row.seed)] = row.team;

const getSeed = (teamName, conf) => {
    const map = conf === 'east' ? eastSeeds : westSeeds;
    const found = map.find(r => r.team === teamName);
    return found ? found.seed : '';
};

const renderMatch = (title, home, away, winner, conf) => {
    const homeSeed = getSeed(home, conf);
    const awaySeed = getSeed(away, conf);
    const homeClass = home === winner ? "winner" : "";
    const awayClass = away === winner ? "winner" : "";
    return `
        <div class="match-card">
            <div class="match-title">${title}</div>
            <div class="team ${homeClass}"><span>${homeSeed ? '#' + homeSeed + ' ' : ''}${home}</span></div>
            <div class="team ${awayClass}"><span>${awaySeed ? '#' + awaySeed + ' ' : ''}${away}</span></div>
        </div>
    `;
};

const wc = bracket.wildcard || {};
const r1 = bracket.round_one || {};
const sf = bracket.conference_semifinals || {};
const cf = bracket.conference_finals || {};
const cup = bracket.mls_cup || {};

const eWC = wc.east || {};
const eR1 = r1.east || {};
const eSF = sf.east || [];
const eCF = cf.east || {};

const wWC = wc.west || {};
const wR1 = r1.west || {};
const wSF = sf.west || [];
const wCF = cf.west || {};

let html = `
    <h3>MLS Cup Playoff Bracket (Projected)</h3>
    <p class="note">Projected based on current standings and model probabilities.</p>
    <div class="bracket-container">
`;

const buildConfBracket = (name, confCode, wcMatch, r1Matches, sfMatches, cfMatch) => {
    return `
        <div class="conference-section">
            <div class="conference-title">${name} Conference</div>
            <div class="bracket-tree">
                <div class="bracket-round">
                    <h4>Wildcard</h4>
                    ${renderMatch("Wildcard", wcMatch.home_team, wcMatch.away_team, wcMatch.winner, confCode)}
                </div>
                <div class="bracket-round">
                    <h4>Round One</h4>
                    ${renderMatch("Best of 3", r1Matches.A.high_seed_team, r1Matches.A.low_seed_team, r1Matches.A.winner, confCode)}
                    ${renderMatch("Best of 3", r1Matches.D.high_seed_team, r1Matches.D.low_seed_team, r1Matches.D.winner, confCode)}
                    ${renderMatch("Best of 3", r1Matches.B.high_seed_team, r1Matches.B.low_seed_team, r1Matches.B.winner, confCode)}
                    ${renderMatch("Best of 3", r1Matches.C.high_seed_team, r1Matches.C.low_seed_team, r1Matches.C.winner, confCode)}
                </div>
                <div class="bracket-round">
                    <h4>Semis</h4>
                    ${renderMatch("Semis", sfMatches[0].home_team, sfMatches[0].away_team, sfMatches[0].winner, confCode)}
                    ${renderMatch("Semis", sfMatches[1].home_team, sfMatches[1].away_team, sfMatches[1].winner, confCode)}
                </div>
                <div class="bracket-round">
                    <h4>Conf. Final</h4>
                    ${renderMatch("Final", cfMatch.home_team, cfMatch.away_team, cfMatch.winner, confCode)}
                </div>
            </div>
        </div>
    `;
};

html += buildConfBracket("Eastern", "east", eWC, eR1, eSF, eCF);
html += buildConfBracket("Western", "west", wWC, wR1, wSF, wCF);

html += `
    <div class="mls-cup-container">
        <div class="mls-cup-card">
            <h4>MLS Cup Final</h4>
            <div class="team ${cup.home_team === cup.winner ? 'winner' : ''}">${cup.home_team}</div>
            <div class="team ${cup.away_team === cup.winner ? 'winner' : ''}">${cup.away_team}</div>
            <div class="mls-cup-winner">Winner: ${cup.winner}</div>
        </div>
    </div>
`;

html += `</div>`;
leagueTableView.innerHTML = html;
}

function renderCupBracket(payload, competition, target = leagueTableView) {
const brackets = payload && payload.cup_brackets && payload.cup_brackets.competitions
    ? payload.cup_brackets.competitions
    : {};
const bracket = brackets[competition];
if (!bracket || !bracket.rounds || !bracket.rounds.length) {
    target.innerHTML = `<p>No cup fixture bracket found for ${escapeHtml(competition)}. Run Track_Cup_Results.py after cup predictions are generated.</p>`;
    return;
}
let html = `
    <h3>${escapeHtml(competition)} Fixture Bracket</h3>
    <p class="note">Built from tracked completed cup predictions and upcoming cup fixtures.</p>
    <div class="bracket-container cup-bracket-container">
`;
for (const round of bracket.rounds) {
    const matches = round.matches || [];
    html += `
    <div class="bracket-round">
        <h4>${escapeHtml(round.name || "Cup Fixtures")}</h4>
    `;
    if (!matches.length) {
    html += "<p>No matches in this section.</p>";
    }
    for (const match of matches) {
    const isCompleted = String(match.status || "").toLowerCase() === "completed";
    const score = isCompleted && match.actual_home_goals !== null && match.actual_away_goals !== null
        ? `${match.actual_home_goals} - ${match.actual_away_goals}`
        : (
        match.pred_home_goals !== null && match.pred_away_goals !== null
            ? `Projected ${match.pred_home_goals} - ${match.pred_away_goals}`
            : "Prediction pending"
        );
    html += `
        <div class="match-card">
        <div class="match-title">${escapeHtml(match.match_date || match.status || "Cup match")}</div>
        <div class="team ${match.home_team === match.winner ? "winner" : ""}"><span>${escapeHtml(match.home_team)}</span></div>
        <div class="team ${match.away_team === match.winner ? "winner" : ""}"><span>${escapeHtml(match.away_team)}</span></div>
        <div class="match-meta"><strong>${escapeHtml(score)}</strong></div>
        <div class="match-meta">Winner: ${escapeHtml(match.winner || "TBD")}</div>
        </div>
    `;
    }
    html += "</div>";
}
html += "</div>";
target.innerHTML = html;
}

function cupConfigForCompetition(competition) {
return cupProjectionConfigs.find((config) => config.aliases.includes(competition)) || cupProjectionConfigs[0];
}

function primaryCupCompetition(config, payload) {
if (!config) return "";
const tables = payload && payload.tables ? payload.tables : {};
const brackets = payload && payload.cup_brackets && payload.cup_brackets.competitions
    ? payload.cup_brackets.competitions
    : {};
return config.aliases.find((name) => tables[name] || brackets[name]) || config.competition;
}

function cupCompetitionHasData(config, payload) {
const competition = primaryCupCompetition(config, payload);
const hasRows = Boolean(payload && payload.tables && payload.tables[competition] && payload.tables[competition].length);
const hasBracket = Boolean(
    payload &&
    payload.cup_brackets &&
    payload.cup_brackets.competitions &&
    payload.cup_brackets.competitions[competition]
);
return hasRows || hasBracket;
}

function renderCupProjectionTabs(payload) {
cupProjectionTabs.innerHTML = cupProjectionConfigs.map((config) => {
    const competition = primaryCupCompetition(config, payload);
    const active = cupConfigForCompetition(activeCupProjectionCompetition).key === config.key;
    const emptyClass = cupCompetitionHasData(config, payload) ? "" : " cup-tab-empty";
    return `
    <button
        class="tab-btn${active ? " active" : ""}${emptyClass}"
        type="button"
        data-cup-projection="${escapeHtml(competition)}"
    >${escapeHtml(config.label)}</button>
    `;
}).join("");
}

function renderCupTable(payload, competition) {
const config = cupConfigForCompetition(competition);
if (!config.hasTable) {
    cupTableView.innerHTML = `<p>${escapeHtml(config.label)} uses a knockout bracket view instead of a league-phase table.</p>`;
    return;
}
const rows = payload && payload.tables ? (payload.tables[competition] || []) : [];
const phaseMatches = config.leaguePhaseMatches || 8;
if (!rows.length) {
    cupTableView.innerHTML = `
    <h3>${escapeHtml(config.label)} League Phase Table</h3>
    <p>No league-phase table data available yet. Run cup predictions and Track_Cup_Results.py to populate this table.</p>
    `;
    return;
}
cupTableView.innerHTML = `
    <h3>${escapeHtml(config.label)} League Phase Table</h3>
    <p class="note">League phase uses ${phaseMatches} matches per club. Top 8 advance to the Round of 16; positions 9-24 enter the first round playoff.</p>
    <div class="stats-row">
    <span class="stat-chip cup-top8-chip">Top 8: Round of 16</span>
    <span class="stat-chip cup-playoff-chip">9-24: First Round Playoff</span>
    </div>
    ${renderLeagueTableRows(rows, competition)}
`;
}

function renderCupProjectionViews() {
const payload = leagueTablesCache.cups;
if (!payload) {
    cupTableView.textContent = "Loading cup projections...";
    cupBracketView.textContent = "Loading cup projections...";
    return;
}
const config = cupConfigForCompetition(activeCupProjectionCompetition);
const competition = primaryCupCompetition(config, payload);
activeCupProjectionCompetition = competition;
renderCupProjectionTabs(payload);
cupFormatNote.textContent = "";
if (!cupCompetitionHasData(config, payload)) {
    cupTableView.innerHTML = "<p>No available data yet. Try again later.</p>";
    cupBracketView.innerHTML = "<p>No available data yet. Try again later.</p>";
    cupViewTable.classList.toggle("active", activeCupProjectionView === "table");
    cupViewBracket.classList.toggle("active", activeCupProjectionView === "bracket");
    cupTableView.classList.toggle("hidden", activeCupProjectionView !== "table");
    cupBracketView.classList.toggle("hidden", activeCupProjectionView !== "bracket");
    return;
}
cupViewTable.classList.toggle("active", activeCupProjectionView === "table");
cupViewBracket.classList.toggle("active", activeCupProjectionView === "bracket");
cupTableView.classList.toggle("hidden", activeCupProjectionView !== "table");
cupBracketView.classList.toggle("hidden", activeCupProjectionView !== "bracket");
renderCupTable(payload, competition);
renderCupBracket(payload, competition, cupBracketView);
}

async function loadCupProjections() {
if (!leagueTablesCache.cups) {
    cupTableView.textContent = "Loading cup projections...";
    cupBracketView.textContent = "Loading cup projections...";
    const resp = await fetch("/api/league-tables?mode=cups");
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
    cupTableView.textContent = "Failed to load cup projections.";
    cupBracketView.textContent = "Failed to load cup projections.";
    if (cupFormatNote) cupFormatNote.textContent = "";
    return;
    }
    leagueTablesCache.cups = data;
    const firstWithData = cupProjectionConfigs.find((config) => cupCompetitionHasData(config, data));
    if (firstWithData) {
    activeCupProjectionCompetition = primaryCupCompetition(firstWithData, data);
    }
}
const activeConfig = cupConfigForCompetition(activeCupProjectionCompetition);
if (!activeConfig.hasTable && activeCupProjectionView === "table") {
    activeCupProjectionView = "bracket";
}
renderCupProjectionViews();
}

async function loadLeagueTables(mode) {
leagueTableView.textContent = "Loading...";
const resp = await fetch(`/api/league-tables?mode=${encodeURIComponent(mode)}`);
const data = await resp.json();
if (!resp.ok || !data.ok) {
    leagueTableView.textContent = "Failed to load league tables.";
    return;
}
leagueTablesCache[mode] = data;
const leagues = data.leagues || [];
const cupBracketCount = data.cup_brackets && data.cup_brackets.competitions
    ? Object.keys(data.cup_brackets.competitions).length
    : 0;
if (!leagues.length && !(mode === "cups" && cupBracketCount)) {
    leagueTableView.innerHTML = "<p>No available Tables yet. Try again later.</p>";
    if (winnerView) winnerView.innerHTML = "<p>No available Tables yet. Try again later.</p>";
    return;
}
setLeagueSelectOptions(tableLeague, leagues, mode === "mls", mode === "cups" ? data.cup_brackets : null, mode);
if ((!tableLeague.value || !tableLeague.value.trim()) && tableLeague.options.length > 0) {
    tableLeague.selectedIndex = 0;
}
if (mode === "mls" && tableLeague.value === "__mls_bracket__") {
    tableViewToggle.disabled = true;
    await renderMlsBracket(data);
} else if (mode === "cups" && tableLeague.value.startsWith("__cup_bracket__:")) {
    tableViewToggle.disabled = true;
    renderCupBracket(data, tableLeague.value.replace("__cup_bracket__:", ""));
} else {
    tableViewToggle.disabled = false;
    renderSelectedLeagueTable();
}
if (winnerDataset && winnerDataset.value === mode) {
    renderWinnerView();
}
}

function renderLeagueStats(leagueStats, selectedLeague) {
const selectedLeagues = Array.isArray(selectedLeague)
    ? selectedLeague.filter(Boolean)
    : (selectedLeague ? [selectedLeague] : []);
if (!selectedLeagues.length || !leagueStats || !leagueStats.length) {
    return "<p>No selected league stats yet.</p>";
}
const rows = leagueStats.filter((item) => selectedLeagues.includes(item.competition));
if (!rows.length) {
    return "<p>No selected league stats yet.</p>";
}
const label = selectedLeagues.length === 1 ? selectedLeagues[0] : rows.map((row) => row.competition).join(" / ");
const correctTotal = rows.reduce((sum, row) => sum + (Number(row.correct_total) || 0), 0);
const settledTotal = rows.reduce((sum, row) => sum + (Number(row.settled_total) || 0), 0);
const accuracyPct = settledTotal ? (100 * correctTotal / settledTotal) : 0;
return `<p><strong>${label} Accuracy:</strong> ${correctTotal}/${settledTotal} (${asPct(accuracyPct)})</p>`;
}

function renderStats(target, stats, leagueStats, selectedLeague) {
target.innerHTML = `
    <h3>Tracking Stats</h3>
    <div class="stats-row">
    <span class="stat-chip">Accuracy ${asPct(stats.accuracy_pct)}</span>
    <span class="stat-chip">Correct ${stats.correct_total}/${stats.settled_total}</span>
    <span class="stat-chip">Pending ${stats.pending_total}</span>
    </div>
    ${renderLeagueStats(leagueStats, selectedLeague)}
`;
}

function escapeHtml(value) {
return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function currentUpcomingSource() {
if (!globalSourceFilter) return "global";
if (globalSourceFilter.value === "mls") return "mls";
if (globalSourceFilter.value === "extra") return "extra";
if (globalSourceFilter.value === "cups") return "cups";
if (globalSourceFilter.value === "world-cup") return "world-cup";
if (globalSourceFilter.value === "friendlies") return "friendlies";
return "global";
}

function upcomingUrlForSource(source) {
if (source === "mls") return "/api/upcoming/mls";
if (source === "extra") return "/api/upcoming/extra";
if (source === "cups") return "/api/upcoming/cups";
if (source === "world-cup") return "/api/upcoming/world-cup";
if (source === "friendlies") return "/api/upcoming/friendlies";
return "/api/upcoming/global";
}

function normalizeLeagueSelection(selectedLeague) {
if (Array.isArray(selectedLeague)) {
    return selectedLeague.map((league) => String(league || "").trim()).filter(Boolean);
}
const league = String(selectedLeague || "").trim();
return league ? [league] : [];
}

function rowsForLeagueSelection(rows, selectedLeague) {
const selectedLeagues = normalizeLeagueSelection(selectedLeague);
if (!selectedLeagues.length) return rows;
return rows.filter((r) => selectedLeagues.includes(r.competition));
}

function renderCupTabs() {
if (!cupTabs) return;
cupTabs.innerHTML = cupPredictionTabs.map((tab) => `
    <option value="${escapeHtml(tab.key)}" ${tab.key === activeCupTab ? "selected" : ""}>${escapeHtml(tab.label)}</option>
`).join("");
}

function activeCupSelection() {
return cupPredictionTabs.find((tab) => tab.key === activeCupTab) || cupPredictionTabs[0];
}

function renderActiveCupTab() {
const tab = activeCupSelection();
renderUpcoming(globalList, upcomingCache.cups, tab.competitions);
const payload = upcomingStatsCache.cups;
renderStats(
    globalStats,
    payload.stats || { correct_total: 0, total_predictions: 0, pending_total: 0, accuracy_pct: 0.0 },
    payload.league_stats || [],
    tab.competitions
);
}

function rowDateIso(row) {
const raw = String(row?.match_date_iso || row?.match_date || "").trim();
if (/^\d{4}-\d{2}-\d{2}/.test(raw)) return raw.slice(0, 10);
const parsed = Date.parse(raw);
if (!Number.isNaN(parsed)) {
    return new Date(parsed).toISOString().slice(0, 10);
}
return "";
}

function predictionQualityLabel(row) {
    const quality = String(row?.prediction_quality || "").toLowerCase();
    if (quality === "provisional") return "Provisional";
    if (quality === "no_prediction" || row?.has_prediction === false) return "No prediction";
    return "Predicted";
}

function predictionQualityPillClass(row) {
    const quality = String(row?.prediction_quality || "").toLowerCase();
    if (quality === "provisional") return "quality-pill provisional-pill";
    if (quality === "no_prediction" || row?.has_prediction === false) return "quality-pill no-prediction-pill";
    return "quality-pill prediction-pill";
}

function liveUpdatesBadgeHtml(row) {
    if (row?.live_updates) {
        return `<div class="confidence-pill live-pill">Live</div>`;
    }
    if (row?.live_status === "qualifying") {
        return `<div class="confidence-pill schedule-pill">Qualifying</div>`;
    }
    if (row?.live_updates_eligible) {
        return `<div class="confidence-pill schedule-pill">No live</div>`;
    }
    return "";
}

function renderUpcoming(target, rows, selectedLeague, options = {}) {
    if (!rows.length) {
        target.innerHTML = "<p>No upcoming matches found.</p>";
        return;
    }
    const visibleRows = rowsForLeagueSelection(rows, selectedLeague);
    if (!visibleRows.length) {
        target.innerHTML = "<p>No upcoming matches for this selection.</p>";
        return;
    }
    const PAGE_SIZE = 30;
    let currentPage = 0;

    const todayStr = new Date().toISOString().slice(0, 10);

    // Upcoming tab hides stale fixtures; home can opt into historical date ranges.
    const futureRows = options.includePast ? visibleRows : visibleRows.filter(r => {
        const d = rowDateIso(r);
        return d && d >= todayStr;
    });

    if (!futureRows.length) {
        target.innerHTML = "<p>No upcoming matches for this selection.</p>";
        return;
    }

    const sortByKickoff = (a, b) => {
            const ta = a.match_datetime_et || "";
            const tb = b.match_datetime_et || "";
            if (ta && tb) return ta < tb ? -1 : ta > tb ? 1 : 0;
            if (ta) return -1;
            if (tb) return 1;
            return String(a.home_team || "").localeCompare(String(b.home_team || ""));
    };

    const dayLabelForRow = (row) => `${row.weekday || ""} ${row.date_label || ""}`.trim();

    const appendDayGroups = (flatItems, groupedRows) => {
        const byDay = {};
        for (const row of groupedRows) {
            const key = dayLabelForRow(row);
            byDay[key] = byDay[key] || [];
            byDay[key].push(row);
        }
        for (const key of Object.keys(byDay)) {
            byDay[key].sort(sortByKickoff);
        }
        const days = Object.keys(byDay).sort((a, b) => {
            const da = byDay[a][0]?.match_datetime_et || byDay[a][0]?.match_date || "";
            const db = byDay[b][0]?.match_datetime_et || byDay[b][0]?.match_date || "";
            if (da && db) return da < db ? -1 : da > db ? 1 : 0;
            return a.localeCompare(b);
        });
        for (const day of days) {
            flatItems.push({ type: "day", label: day });
            for (const row of byDay[day]) {
                flatItems.push({ type: "match", data: row });
            }
        }
    };

    // Flatten with group separators for pagination.
    const flatItems = [];
    if (options.groupByLeague) {
        const byLeague = {};
        for (const row of futureRows) {
            const league = row.competition || "Other";
            byLeague[league] = byLeague[league] || [];
            byLeague[league].push(row);
        }
        for (const league of Object.keys(byLeague).sort((a, b) => a.localeCompare(b))) {
            flatItems.push({ type: "league", label: league });
            appendDayGroups(flatItems, byLeague[league]);
        }
    } else {
        appendDayGroups(flatItems, futureRows);
    }
    const totalPages = Math.ceil(flatItems.length / PAGE_SIZE);

    function renderPage(page) {
        const start = page * PAGE_SIZE;
        const end = Math.min(start + PAGE_SIZE, flatItems.length);
        const fragment = document.createDocumentFragment();
        let currentGrid = null;
        
        for (let i = start; i < end; i++) {
            const item = flatItems[i];
            if (item.type === 'league') {
                const div = document.createElement('div');
                div.className = 'league-title';
                div.textContent = item.label;
                fragment.appendChild(div);
                currentGrid = null;
            } else if (item.type === 'day') {
                const div = document.createElement('div');
                div.className = 'day-title';
                div.textContent = item.label;
                fragment.appendChild(div);
                const grid = document.createElement('div');
                grid.className = 'kick-card-grid';
                fragment.appendChild(grid);
                currentGrid = grid;
            } else {
                const r = item.data;
                if (!currentGrid) {
                    currentGrid = document.createElement('div');
                    currentGrid.className = 'kick-card-grid';
                    fragment.appendChild(currentGrid);
                }
                
                const article = document.createElement('article');
                const scheduleOnly = Boolean(r.schedule_only);
                const hasPrediction = Boolean(r.has_prediction) && !scheduleOnly;
                const noPrediction = scheduleOnly || String(r.prediction_quality || "").toLowerCase() === "no_prediction" || !hasPrediction;
                const qualityPill = `<div class="${predictionQualityPillClass(r)}">${escapeHtml(predictionQualityLabel(r))}</div>`;
                const livePill = liveUpdatesBadgeHtml(r);
                const hasFinalScore = r.actual_home_goals !== null && r.actual_home_goals !== undefined
                    && r.actual_away_goals !== null && r.actual_away_goals !== undefined;
                const homeGoals = (r.pred_home_goals === null || r.pred_home_goals === undefined) ? "NA" : r.pred_home_goals;
                const awayGoals = (r.pred_away_goals === null || r.pred_away_goals === undefined) ? "NA" : r.pred_away_goals;
                const settled = String(r.actual_result || "").trim().match(/^[HDA]$/i);
                const isCorrect = String(r.is_correct || "").trim().toLowerCase();
                let rowClass = "";
                let statusText = noPrediction
                    ? (hasFinalScore ? "Final" : "Scheduled")
                    : "Pending";
                if (!noPrediction && settled) {
                    if (isCorrect === "1" || isCorrect === "true") {
                        rowClass = "match-correct";
                        statusText = "Correct";
                    } else {
                        rowClass = "match-wrong";
                        statusText = "Wrong";
                    }
                } else if (!noPrediction && hasFinalScore) {
                    statusText = "Final";
                }
                
                article.className = `match-row kick-match-card ${rowClass}`;
                if (noPrediction) {
                    article.innerHTML = `
                    <button class="match-toggle" type="button"
                        data-home-team="${escapeHtml(r.home_team)}"
                        data-away-team="${escapeHtml(r.away_team)}"
                        aria-label="Open ${escapeHtml(r.home_team)} vs ${escapeHtml(r.away_team)} head to head">
                        <div class="kick-head">
                            <div class="kick-league">${escapeHtml(r.competition)}</div>
                            <div class="kick-head-pills">${qualityPill}${livePill}</div>
                        </div>
                        <div class="matchup">${escapeHtml(r.home_team)} vs ${escapeHtml(r.away_team)}</div>
                        ${r.prediction_note ? `<div class="match-meta prediction-note">${escapeHtml(r.prediction_note)}</div>` : ""}
                        ${r.time_label ? `<div class="match-meta"><strong>Kickoff:</strong> ${escapeHtml(r.time_label)}</div>` : ""}
                        ${hasFinalScore
                            ? `<div class="match-meta"><strong>Final score:</strong> ${escapeHtml(r.home_team)} ${r.actual_home_goals} - ${r.actual_away_goals} ${escapeHtml(r.away_team)}</div>`
                            : `<div class="match-meta"><strong>Status:</strong> Scheduled</div>`}
                        <div class="match-meta"><strong>Click:</strong> Open head to head</div>
                    </button>
                `;
                } else {
                article.innerHTML = `
                    <button class="match-toggle" type="button"
                        data-home-team="${escapeHtml(r.home_team)}"
                        data-away-team="${escapeHtml(r.away_team)}"
                        aria-label="Open ${escapeHtml(r.home_team)} vs ${escapeHtml(r.away_team)} head to head">
                        <div class="kick-head">
                            <div class="kick-league">${escapeHtml(r.competition)}</div>
                            <div class="kick-head-pills">${qualityPill}${livePill}<div class="confidence-pill">${pctLabel(Math.max(Number(r.prob_home) || 0, Number(r.prob_draw) || 0, Number(r.prob_away) || 0))}% confidence</div></div>
                        </div>
                        <div class="matchup">${escapeHtml(r.home_team)} vs ${escapeHtml(r.away_team)}</div>
                        <div class="match-meta">Prediction: <span class="winner-line">${escapeHtml(r.winner_label)}</span></div>
                        ${r.time_label ? `<div class="match-meta"><strong>Kickoff:</strong> ${escapeHtml(r.time_label)}</div>` : ""}
                        <div class="match-meta"><strong>Predicted score:</strong> ${escapeHtml(r.home_team)} ${homeGoals} - ${awayGoals} ${escapeHtml(r.away_team)}</div>
                        ${hasFinalScore
                            ? `<div class="match-meta"><strong>Final score:</strong> ${escapeHtml(r.home_team)} ${r.actual_home_goals} - ${r.actual_away_goals} ${escapeHtml(r.away_team)}</div>`
                            : ""}
                        <div class="probability-track">
                            <div style="width: ${r.prob_home}%;" title="${escapeHtml(r.home_team)}"></div>
                            <div style="width: ${r.prob_draw}%;" title="Draw"></div>
                            <div style="width: ${r.prob_away}%;" title="${escapeHtml(r.away_team)}"></div>
                        </div>
                        <div class="probability-labels">
                            <span>H: ${pctLabel(r.prob_home)}%</span> <span>D: ${pctLabel(r.prob_draw)}%</span> <span>A: ${pctLabel(r.prob_away)}%</span>
                        </div>
                        <div class="match-meta"><strong>Status:</strong> ${statusText}</div>
                        <div class="match-meta"><strong>Click:</strong> Open head to head</div>
                    </button>
                `;
                }
                currentGrid.appendChild(article);
            }
        }
        
        target.innerHTML = '';
        target.appendChild(fragment);
        
        // Add pagination controls
        if (totalPages > 1) {
            const pagination = document.createElement('div');
            pagination.className = 'pagination-controls';
            pagination.style.cssText = 'display:flex;justify-content:center;gap:8px;margin-top:16px;padding:8px;';
            pagination.innerHTML = `
                <button class="page-btn" data-page="${Math.max(0, currentPage - 1)}" ${currentPage === 0 ? 'disabled' : ''}>← Prev</button>
                <span class="page-info">Page ${currentPage + 1} of ${totalPages}</span>
                <button class="page-btn" data-page="${Math.min(totalPages - 1, currentPage + 1)}" ${currentPage === totalPages - 1 ? 'disabled' : ''}>Next →</button>
            `;
            target.appendChild(pagination);
            
            pagination.querySelectorAll('.page-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const page = parseInt(btn.dataset.page, 10);
                    if (!isNaN(page) && page !== currentPage) {
                        currentPage = page;
                        renderPage(currentPage);
                    }
                });
            });
        }
    }

    renderPage(currentPage);
}

function toConfidence(row) {
return Math.max(Number(row.prob_home) || 0, Number(row.prob_draw) || 0, Number(row.prob_away) || 0);
}

function pickRandomRows(rows, count) {
const copy = [...rows];
for (let i = copy.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    const tmp = copy[i];
    copy[i] = copy[j];
    copy[j] = tmp;
}
return copy.slice(0, Math.min(count, copy.length));
}

function isValidProbabilityRow(row) {
if (!row || !row.home_team || !row.away_team || !row.winner_label) return false;
const h = Number(row.prob_home);
const d = Number(row.prob_draw);
const a = Number(row.prob_away);
if (![h, d, a].every(Number.isFinite)) return false;
if (h < 0 || d < 0 || a < 0) return false;
return true;
}

function isLikelyFutureFixture(row) {
const raw = String(row?.match_date || "").trim();
if (!raw) return false;
const parsed = Date.parse(raw);
if (Number.isNaN(parsed)) return true;
const today = new Date();
today.setHours(0, 0, 0, 0);
return parsed >= today.getTime();
}

function dedupeFixtures(rows) {
const seen = new Set();
const out = [];
for (const r of rows) {
    const key = [
    String(r.match_date || "").trim(),
    String(r.competition || "").trim(),
    String(r.home_team || "").trim().toLowerCase(),
    String(r.away_team || "").trim().toLowerCase(),
    ].join("|");
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(r);
}
return out;
}

function renderTopPicks(rows) {
    if (!topPicksList) return;
    // Prefer rows passed in by the server-side `/api/top-picks` endpoint
    // (sorted by confidence desc, capped to N). Fall back to a client-side
    // random pick from the full upcoming cache for older pages that still
    // call renderTopPicks() with no argument.
    let picks = [];
    if (Array.isArray(rows) && rows.length) {
        picks = rows.slice(0, 12);
    } else {
        const allRows = dedupeFixtures(
            [
                ...(upcomingCache.global || []),
                ...(upcomingCache.mls || []),
                ...(upcomingCache.extra || []),
                ...(upcomingCache.cups || []),
                ...(upcomingCache.worldcup || []),
            ]
                .filter(isValidProbabilityRow)
        );
        const futureRows = allRows.filter(isLikelyFutureFixture);
        const sourceRows = futureRows.length ? futureRows : allRows;
        picks = pickRandomRows(sourceRows, 12);
    }
    if (!picks.length) {
    topPicksList.innerHTML = "<p class=\"muted-placeholder\">No upcoming games</p>";
    return;
    }
    topPicksList.innerHTML = picks.map((r) => `
    <button
        class="pick-card match-toggle"
        type="button"
        data-home-team="${escapeHtml(r.home_team)}"
        data-away-team="${escapeHtml(r.away_team)}"
        aria-label="Open ${escapeHtml(r.home_team)} vs ${escapeHtml(r.away_team)} head to head"
    >
        <p class="pick-league">${escapeHtml(r.competition)}</p>
        <p class="pick-match">${escapeHtml(r.home_team)} vs ${escapeHtml(r.away_team)}</p>
        <p class="match-meta">${escapeHtml(`${r.weekday || ""} ${r.date_label || ""}`.trim())}${r.time_label ? ` - ${escapeHtml(r.time_label)}` : ""}</p>
        <p class="pick-prediction">Prediction: ${escapeHtml(r.winner_label)}</p>
        <div class="probability-track">
        <div style="width: ${Number(r.prob_home) || 0}%;" title="${escapeHtml(r.home_team)}"></div>
        <div style="width: ${Number(r.prob_draw) || 0}%;" title="Draw"></div>
        <div style="width: ${Number(r.prob_away) || 0}%;" title="${escapeHtml(r.away_team)}"></div>
        </div>
        <div class="probability-labels">
        <span>H: ${pctLabel(r.prob_home)}%</span>
        <span>D: ${pctLabel(r.prob_draw)}%</span>
        <span>A: ${pctLabel(r.prob_away)}%</span>
        </div>
        <p class="pick-confidence">Confidence: ${pctLabel(toConfidence(r))}%</p>
    </button>
    `).join("");
}

async function loadHomeStats() {
    try {
        const resp = await fetch("/api/stats");
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data?.ok) return;
        const leaguesEl = document.getElementById("hero-leagues");
        const refreshedEl = document.getElementById("hero-refreshed");
        if (leaguesEl && data.league_count != null) {
            leaguesEl.textContent = `${data.league_count}+`;
        }
        if (refreshedEl && data.refreshed_at) {
            const d = new Date(data.refreshed_at);
            const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZone: "America/New_York" });
            const date = d.toLocaleDateString([], { month: "short", day: "numeric", timeZone: "America/New_York" });
            refreshedEl.textContent = `${date} ${time}`;
        }
    } catch (_err) {
        // stats are non-critical
    }
}

async function preloadHomeData() {
    const isHomePage = document.body?.dataset?.activePage === "home";
    if (!isHomePage || !topPicksList) {
        return;
    }
    loadHomeStats();
    try {
        const resp = await fetch("/api/top-picks?limit=12");
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data?.ok) {
            renderTopPicks(data.rows || []);
            return;
        }
        console.error("Top-picks endpoint returned non-ok response", data);
    } catch (err) {
        console.error("Failed to fetch top picks", err);
    }
}


function getUpcomingLeaguesForSource(source) {
    if (source === "mls") return ["United States/MLS"];
    if (source === "friendlies") return [...FRIENDLIES_OPTIONS];
    return getLeaguesForSource(source);
}

function populateUpcomingLeagueFilter(selectEl, rows, availableLeagues = []) {
// Always use the hardcoded per-source list so all supported leagues are
// available in the dropdown, even if the current data load has no rows for them.
const source = currentUpcomingSource ? currentUpcomingSource() : "global";
let leagues = getUpcomingLeaguesForSource(source);
// For MLS/extra, merge API-reported competition names so upcoming rows are filterable.
if ((source === "mls" || source === "extra") && availableLeagues.length) {
    const merged = new Set([...availableLeagues, ...leagues]);
    leagues = [...merged];
}
const priorityLeagues = [
    "England/Premier League",
    "England/Championship"
];
const leagueRank = (name) => {
    const idx = priorityLeagues.indexOf(name);
    return idx >= 0 ? idx : 1000;
};
leagues.sort((a, b) => {
    const ra = leagueRank(a);
    const rb = leagueRank(b);
    if (ra !== rb) return ra - rb;
    return a.localeCompare(b);
});
selectEl.innerHTML = "";
if (!leagues.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No leagues";
    selectEl.appendChild(option);
    return "";
}
for (const league of leagues) {
    const option = document.createElement("option");
    option.value = league;
    option.textContent = league;
    selectEl.appendChild(option);
}
return leagues[0];
}

async function loadUpcoming(mode, url, target, statsTarget, filterEl) {
target.textContent = "Loading...";
if (statsTarget) statsTarget.innerHTML = "";
const isCupMode = mode === "cups";
const isFriendliesMode = mode === "friendlies";
cupTabs.classList.toggle("hidden", !isCupMode);
const cupLabel = document.querySelector('label[for="cup-tabs"]');
if (cupLabel) cupLabel.classList.toggle("hidden", !isCupMode);
globalLeagueFilterCard.classList.toggle("hidden", isFriendliesMode);
if (statsTarget) statsTarget.classList.toggle("hidden", isFriendliesMode);
const resp = await fetch(url);
const data = await resp.json();
if (!resp.ok || !data.ok) {
    target.textContent = "Failed to load upcoming predictions.";
    return;
}
const rows = data.rows || [];
upcomingCache[mode] = rows;
upcomingStatsCache[mode] = {
    stats: data.stats || null,
    league_stats: data.league_stats || [],
};
const selectedLeague = isFriendliesMode
    ? ""
    : populateUpcomingLeagueFilter(filterEl, rows, data.available_leagues || []);
if (isCupMode) {
    renderCupTabs();
}
renderUpcoming(target, rows, selectedLeague);
renderTopPicks();
}



function inferH2HMode(team1, team2) {
const t1 = String(team1 || "").trim().toLowerCase();
const t2 = String(team2 || "").trim().toLowerCase();
if (t1 && t2 && mlsTeamSet.has(t1) && mlsTeamSet.has(t2)) return "mls";
if (t1 && t2 && extraTeamSet.has(t1) && extraTeamSet.has(t2)) return "extra";
return "global";
}

function applyH2HDataset(mode) {
const dataset = mode === "mls" ? "mls" : mode === "extra" ? "extra" : "global";
h2hDataset.value = dataset;
const listId = dataset === "mls" ? "mls-teams" : dataset === "extra" ? "extra-teams" : "teams";
h2hTeam1Input.setAttribute("list", listId);
h2hTeam2Input.setAttribute("list", listId);
}

function openMatchupInH2H(homeTeam, awayTeam, mode) {
if (!homeTeam || !awayTeam) return;
const resolvedMode = mode || inferH2HMode(homeTeam, awayTeam);
applyH2HDataset(resolvedMode);
activateTab("h2h");
h2hTeam1Input.value = homeTeam;
h2hTeam2Input.value = awayTeam;
document.getElementById("btn-compare").click();
}

// H2H Logic
const h2hCompareButton = document.getElementById("btn-compare");
if (h2hCompareButton) {
h2hCompareButton.addEventListener("click", async () => {
    const t1 = h2hTeam1Input.value;
    const t2 = h2hTeam2Input.value;
    if(!t1 || !t2) return showNotification("Please select two teams.");
    const dataset = h2hDataset.value || inferH2HMode(t1, t2);

    h2hResults.innerHTML = "Loading...";
    h2hResults.classList.remove("hidden");

    // Fetch head-to-head + match prediction in parallel so the user gets both at once.
    let h2hData = null;
    let prediction = null;
    let h2hError = null;
    let predictionError = null;
    const requests = await Promise.allSettled([
        fetch(`/api/h2h?team1=${encodeURIComponent(t1)}&team2=${encodeURIComponent(t2)}&mode=${encodeURIComponent(dataset)}`).then((r) => r.json()),
        fetch("/api/predict", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ home_team: t1, away_team: t2, mode: dataset }),
        }).then((r) => r.json()),
    ]);
    if (requests[0].status === "fulfilled" && requests[0].value && requests[0].value.ok) {
        h2hData = requests[0].value;
    } else {
        h2hError = (requests[0].status === "fulfilled" && requests[0].value && requests[0].value.error) || (requests[0].reason && requests[0].reason.message) || "Failed to load head to head data.";
    }
    if (requests[1].status === "fulfilled" && requests[1].value && requests[1].value.ok) {
        prediction = requests[1].value.prediction;
    } else {
        predictionError = (requests[1].status === "fulfilled" && requests[1].value && requests[1].value.error) || (requests[1].reason && requests[1].reason.message) || "Prediction unavailable for this matchup.";
    }

    const renderTeamHeaderRow = (leftName, rightName) => `
        <div class="stat-row stat-header-row">
            <span class="stat-val">${leftName || "-"}</span>
            <span class="stat-label">Stat</span>
            <span class="stat-val">${rightName || "-"}</span>
        </div>`;

    const renderStatRow = (label, v1, v2) => `
        <div class="stat-row">
            <span class="stat-val">${v1 !== undefined ? v1 : '-'}</span>
            <span class="stat-label">${label}</span>
            <span class="stat-val">${v2 !== undefined ? v2 : '-'}</span>
        </div>`;

    const renderH2HColumn = () => {
        if (!h2hData) return `<div class="h2h-col card"><h3>Head to Head</h3><p class="error">${escapeHtml(h2hError || "Failed to load head to head data.")}</p></div>`;
        const f1 = h2hData.team1_form || {};
        const f2 = h2hData.team2_form || {};
        const h2h = h2hData.h2h_data || {};
        const h2hRev = h2hData.h2h_data_reverse || {};
        return `
            <div class="h2h-col card">
                <h3 style="text-align: center;">Recent Form Comparison</h3>
                ${renderTeamHeaderRow(t1, t2)}
                <h4 style="margin-top: 15px; text-align: center;">Recent Form (Last 10)</h4>
                ${renderStatRow("Points", f1.points_last_10, f2.points_last_10)}
                ${renderStatRow("Wins", f1.wins_last_10, f2.wins_last_10)}
                ${renderStatRow("Draws", f1.draws_last_10, f2.draws_last_10)}
                ${renderStatRow("Losses", f1.losses_last_10, f2.losses_last_10)}
                ${renderStatRow("Goals For (Avg)", f1.avg_goals_for_last_10, f2.avg_goals_for_last_10)}
                ${renderStatRow("Goals Against (Avg)", f1.avg_goals_against_last_10, f2.avg_goals_against_last_10)}
                ${(f1.avg_shots_for_last_10 !== null || f2.avg_shots_for_last_10 !== null)
                ? renderStatRow("Shots For (Avg)", f1.avg_shots_for_last_10, f2.avg_shots_for_last_10)
                : ""}
                ${(f1.avg_shots_against_last_10 !== null || f2.avg_shots_against_last_10 !== null)
                ? renderStatRow("Shots Against (Avg)", f1.avg_shots_against_last_10, f2.avg_shots_against_last_10)
                : ""}
            </div>
            <div class="h2h-col card">
                <h3>Head to Head History</h3>
                <p><strong>${t1} vs ${t2}</strong></p>
                <p><strong>Fixture Location:</strong> ${t1} (Home) vs ${t2} (Away)</p>
                <p>Matches Recorded: ${h2hData.h2h_total_games || 0}</p>
                <div style="margin-top: 10px;">
                    <p>When ${t1} is Home:</p>
                    <ul>
                        <li>${t1} Wins: ${h2h.home_wins || 0}</li>
                        <li>Draws: ${h2h.home_draws || 0}</li>
                        <li>${t2} Wins: ${h2h.home_losses || 0}</li>
                    </ul>
                </div>
                <div style="margin-top: 10px;">
                    <p>When ${t2} is Home:</p>
                    <ul>
                        <li>${t2} Wins: ${h2hRev.home_wins || 0}</li>
                        <li>Draws: ${h2hRev.home_draws || 0}</li>
                        <li>${t1} Wins: ${h2hRev.home_losses || 0}</li>
                    </ul>
                </div>
            </div>
        `;
    };

    const renderPredictionCard = () => {
        if (!prediction) {
            return `<div class="h2h-col card"><h3>Match Prediction</h3><p class="muted-placeholder">${escapeHtml(predictionError || "Prediction unavailable.")}</p></div>`;
        }
        const confidence = Math.max(Number(prediction.prob_home) || 0, Number(prediction.prob_draw) || 0, Number(prediction.prob_away) || 0);
        return `
            <div class="h2h-col card">
                <h3>Match Prediction</h3>
                <p><strong>${escapeHtml(t1)} (H) vs ${escapeHtml(t2)} (A)</strong></p>
                <p class="winner-line">${escapeHtml(prediction.winner_label || "Draw")}</p>
                <p><strong>Predicted score:</strong> ${escapeHtml(prediction.home_team)} ${prediction.pred_home_goals} - ${prediction.pred_away_goals} ${escapeHtml(prediction.away_team)}</p>
                <p><strong>Confidence:</strong> ${pctLabel(confidence)}%</p>
                <div class="probability-track">
                    <div style="width: ${prediction.prob_home}%;" title="${escapeHtml(prediction.home_team)}"></div>
                    <div style="width: ${prediction.prob_draw}%;" title="Draw"></div>
                    <div style="width: ${prediction.prob_away}%;" title="${escapeHtml(prediction.away_team)}"></div>
                </div>
                <div class="probability-labels">
                    <span>H: ${pctLabel(prediction.prob_home)}%</span>
                    <span>D: ${pctLabel(prediction.prob_draw)}%</span>
                    <span>A: ${pctLabel(prediction.prob_away)}%</span>
                </div>
                ${prediction.pred_home_shots !== undefined ? `<p><strong>Shots:</strong> ${prediction.pred_home_shots} - ${prediction.pred_away_shots}</p>` : ""}
            </div>
        `;
    };

    h2hResults.innerHTML = `<div class="h2h-container">${renderH2HColumn()}${renderPredictionCard()}</div>`;
});
}

if (h2hDataset) {
h2hDataset.addEventListener("change", () => {
applyH2HDataset(h2hDataset.value);
});
}

if (tabHome) {
tabHome.addEventListener("click", () => activateTab("home"));
}
if (brandHomeBtn && tabHome) {
brandHomeBtn.addEventListener("click", () => tabHome.click());
}
if (feedbackSubmit) {
feedbackSubmit.addEventListener("click", submitFeedback);
}
if (tabGlobal) {
tabGlobal.addEventListener("click", async () => {
activateTab("global");
const source = currentUpcomingSource();
await loadUpcoming(source, upcomingUrlForSource(source), globalList, globalStats, globalLeagueFilter);
});
}
if (tabCups) {
tabCups.addEventListener("click", async () => {
activateTab("cups");
await loadCupProjections();
});
}
if (tabH2H) {
tabH2H.addEventListener("click", () => activateTab("h2h"));
}
if (tabLeagueTable) {
tabLeagueTable.addEventListener("click", async () => {
activateTab("league-table");
if (!leagueTablesCache[tableDataset.value]) {
    await loadLeagueTables(tableDataset.value);
} else {
    const cached = leagueTablesCache[tableDataset.value];
    setLeagueSelectOptions(
    tableLeague,
    cached.leagues || [],
    tableDataset.value === "mls",
    tableDataset.value === "cups" ? cached.cup_brackets : null,
    tableDataset.value
    );
    if (tableDataset.value === "mls" && tableLeague.value === "__mls_bracket__") {
    tableViewToggle.disabled = true;
    if (tablePositionOddsToggle) tablePositionOddsToggle.disabled = true;
    await renderMlsBracket(cached);
    } else if (tableDataset.value === "cups" && tableLeague.value.startsWith("__cup_bracket__:")) {
    tableViewToggle.disabled = true;
    if (tablePositionOddsToggle) tablePositionOddsToggle.disabled = true;
    renderCupBracket(cached, tableLeague.value.replace("__cup_bracket__:", ""));
    } else {
    tableViewToggle.disabled = false;
    if (tablePositionOddsToggle) tablePositionOddsToggle.disabled = false;
    renderSelectedLeagueTable();
    }
}
});
}
if (tabPlayers) {
tabPlayers.addEventListener("click", () => {
window.location.href = "/players";
});
}
if (tabTactics) {
tabTactics.addEventListener("click", () => {
window.location.href = "/tactics";
});
}
if (headerAbout) {
headerAbout.addEventListener("click", () => {
    window.location.href = "/about";
});
}
if (tableDataset) {
tableDataset.addEventListener("change", async () => {
await loadLeagueTables(tableDataset.value);
});
}
// bind winner change handler only on pages that render the winner widget
if (winnerDataset) {
winnerDataset.addEventListener("change", async () => {
    if (!leagueTablesCache[winnerDataset.value]) {
    await loadLeagueTables(winnerDataset.value);
    }
    renderWinnerView();
});
}
if (tableLeague) {
tableLeague.addEventListener("change", async () => {
if (tableDataset.value === "mls" && tableLeague.value === "__mls_bracket__") {
    tableViewToggle.disabled = true;
    if (tablePositionOddsToggle) tablePositionOddsToggle.disabled = true;
    await renderMlsBracket(leagueTablesCache["mls"] || { tables: {} });
} else if (tableDataset.value === "cups" && tableLeague.value.startsWith("__cup_bracket__:")) {
    tableViewToggle.disabled = true;
    if (tablePositionOddsToggle) tablePositionOddsToggle.disabled = true;
    renderCupBracket(leagueTablesCache["cups"] || { tables: {}, cup_brackets: null }, tableLeague.value.replace("__cup_bracket__:", ""));
} else {
    tableViewToggle.disabled = false;
    if (tablePositionOddsToggle) tablePositionOddsToggle.disabled = false;
    renderSelectedLeagueTable();
}
});
}
if (tableViewToggle) {
tableViewToggle.addEventListener("click", () => {
tableViewMode = tableViewMode === "standings" ? "probability" : "standings";
updateTableViewToggleLabel();
if (tableDataset.value === "mls" && tableLeague.value === "__mls_bracket__") {
    return;
}
renderSelectedLeagueTable();
});
}
if (tablePositionOddsToggle) {
tablePositionOddsToggle.addEventListener("click", () => {
    tablePositionOddsMode = !tablePositionOddsMode;
    updateTablePositionOddsToggleLabel();
    // Position-odds and the simple probability view are alternatives — flip
    // the probability toggle off when turning position-odds on so the user
    // never sees stacked views.
    if (tablePositionOddsMode && tableViewMode === "probability" && tableViewToggle) {
        tableViewMode = "standings";
        updateTableViewToggleLabel();
    }
    if (tableDataset.value === "mls" && tableLeague.value === "__mls_bracket__") {
        return;
    }
    renderSelectedLeagueTable();
});
updateTablePositionOddsToggleLabel();
}
if (cupProjectionTabs) {
cupProjectionTabs.addEventListener("click", async (event) => {
const btn = event.target.closest("[data-cup-projection]");
if (!btn) return;
activeCupProjectionCompetition = btn.getAttribute("data-cup-projection") || activeCupProjectionCompetition;
const config = cupConfigForCompetition(activeCupProjectionCompetition);
if (!config.hasTable) {
    activeCupProjectionView = "bracket";
}
await loadCupProjections();
});
}
if (cupViewTable) {
cupViewTable.addEventListener("click", () => {
const config = cupConfigForCompetition(activeCupProjectionCompetition);
activeCupProjectionView = config.hasTable ? "table" : "bracket";
renderCupProjectionViews();
});
}
if (cupViewBracket) {
cupViewBracket.addEventListener("click", () => {
activeCupProjectionView = "bracket";
renderCupProjectionViews();
});
}
if (globalLeagueFilter) {
globalLeagueFilter.addEventListener("change", () => {
const source = currentUpcomingSource();
if (source === "cups") {
    renderActiveCupTab();
    return;
}
renderUpcoming(globalList, upcomingCache[source], globalLeagueFilter.value);
const payload = upcomingStatsCache[source];
renderStats(
    globalStats,
    payload.stats || { correct_total: 0, total_predictions: 0, pending_total: 0, accuracy_pct: 0.0 },
    payload.league_stats || [],
    globalLeagueFilter.value
);
});
}
if (globalSourceFilter) {
globalSourceFilter.addEventListener("change", async () => {
const source = currentUpcomingSource();
await loadUpcoming(source, upcomingUrlForSource(source), globalList, globalStats, globalLeagueFilter);
});
}
if (cupTabs) {
cupTabs.addEventListener("change", () => {
activeCupTab = cupTabs.value || "all";
renderCupTabs();
renderActiveCupTab();
});
}

if (globalList) {
globalList.addEventListener("click", (event) => {
const btn = event.target.closest(".match-toggle");
if (!btn) return;
openMatchupInH2H(btn.getAttribute("data-home-team"), btn.getAttribute("data-away-team"));
});
}
if (topPicksList) {
topPicksList.addEventListener("click", (event) => {
const btn = event.target.closest(".match-toggle");
if (!btn) return;
openMatchupInH2H(btn.getAttribute("data-home-team"), btn.getAttribute("data-away-team"));
});
}

// Page bootstrap so direct page loads hydrate their own data without manual tab toggles.
const ACTIVE_PAGE = (document.body?.dataset?.activePage || "home").trim();
updateTableViewToggleLabel();
activateTab(ACTIVE_PAGE);

function getLeagueTableUrlParams() {
    const params = new URLSearchParams(window.location.search);
    return {
        dataset: params.get("dataset"),
        league: params.get("league"),
    };
}

function applyLeagueTableSelection(leagueName) {
    if (!tableLeague || !leagueName) return false;
    const option = Array.from(tableLeague.options).find((entry) => entry.value === leagueName);
    if (!option) return false;
    tableLeague.value = leagueName;
    return true;
}

async function renderLeagueTableSelection() {
    if (!tableDataset || !tableLeague) return;
    if (tableDataset.value === "mls" && tableLeague.value === "__mls_bracket__") {
        tableViewToggle.disabled = true;
        if (tablePositionOddsToggle) tablePositionOddsToggle.disabled = true;
        await renderMlsBracket(leagueTablesCache.mls || { tables: {} });
        return;
    }
    if (tableDataset.value === "cups" && tableLeague.value.startsWith("__cup_bracket__:")) {
        tableViewToggle.disabled = true;
        if (tablePositionOddsToggle) tablePositionOddsToggle.disabled = true;
        renderCupBracket(
            leagueTablesCache.cups || { tables: {}, cup_brackets: null },
            tableLeague.value.replace("__cup_bracket__:", "")
        );
        return;
    }
    tableViewToggle.disabled = false;
    if (tablePositionOddsToggle) tablePositionOddsToggle.disabled = false;
    renderSelectedLeagueTable();
}

async function initializeActivePage() {
if (ACTIVE_PAGE === "global") {
    if (!globalList || !globalStats || !globalLeagueFilter || !globalSourceFilter) return;
    const source = currentUpcomingSource();
    await loadUpcoming(source, upcomingUrlForSource(source), globalList, globalStats, globalLeagueFilter);
} else if (ACTIVE_PAGE === "cups") {
    if (document.getElementById("cup-projection-view")) return;
    if (!cupProjectionTabs) return;
    await loadCupProjections();
} else if (ACTIVE_PAGE === "league-table") {
    if (!tableDataset || !tableLeague) return;
    const urlParams = getLeagueTableUrlParams();
    if (urlParams.dataset && ["global", "mls", "extra", "world-cup", "cups"].includes(urlParams.dataset)) {
        tableDataset.value = urlParams.dataset;
    }
    await loadLeagueTables(tableDataset.value);
    if (urlParams.league) {
        applyLeagueTableSelection(urlParams.league);
    }
    if (tableLeague.value) {
        await renderLeagueTableSelection();
    }
}
}

initializeActivePage();
preloadHomeData();
