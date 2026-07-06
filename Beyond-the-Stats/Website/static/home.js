// preloadHomeData is invoked from shared.js so upcoming cards and summary stats render on every page

// build a winner row from a league table payload
function pickLeagueWinner(rows) {
    if (!Array.isArray(rows) || !rows.length) return null;
    return [...rows].sort((left, right) => {
        const leftWin = Number(left?.win_league_pct) || 0;
        const rightWin = Number(right?.win_league_pct) || 0;
        if (rightWin !== leftWin) return rightWin - leftWin;
        return (Number(left?.position) || 999) - (Number(right?.position) || 999);
    })[0];
}

// render winners from a single dataset payload.
function renderHomeWinners(data) {
    if (!winnerView) return;
    const tables = data?.tables || {};
    const leagues = Object.keys(tables).filter((name) => name !== "__mls_bracket__").sort((a, b) => a.localeCompare(b));
    if (!leagues.length) return "";
    return leagues.map((league) => {
        const winner = pickLeagueWinner(tables[league]);
        return `<tr><td>${escapeHtmlText(league)}</td><td>${winner ? escapeHtmlText(winner.team) : "N/A"}</td><td>${winner ? asPct(winner.win_league_pct) : "0%"}</td></tr>`;
    }).join("");
}

// fetch every available dataset and render all winners into a single combined table.
async function loadHomeWinners() {
    if (!winnerView) return;
    winnerView.innerHTML = "Loading league winners...";
    const sources = ["global", "mls", "extra"];
    try {
        const payloads = await Promise.all(sources.map(async (mode) => {
            try {
                const response = await fetch(`/api/league-tables?mode=${encodeURIComponent(mode)}`);
                const data = await response.json();
                if (!response.ok || !data?.ok) return null;
                return { mode, data };
            } catch (_error) {
                return null;
            }
        }));
        const rows = payloads
            .filter((entry) => entry)
            .map((entry) => renderHomeWinners(entry.data))
            .filter(Boolean)
            .join("");
        if (!rows) {
            winnerView.textContent = "No winner data available.";
            return;
        }
        winnerView.innerHTML = `<table class="league-table"><thead><tr><th>League</th><th>Predicted Winner</th><th>Win Chance</th></tr></thead><tbody>${rows}</tbody></table>`;
    } catch (_error) {
        winnerView.textContent = "Failed to load league winners.";
    }
}

// ===== World Cup section on the home page =====
//
// The home page surfaces two compact WC widgets above the existing fixtures + league
// winners: the top 8 title chances (aggregate from 1000 sims) and the 12 projected
// group tables (from the single sim whose champion matches the highest-odds winner).
const wcWinnerOddsView = document.getElementById("wc-winner-odds");
const wcProjectedGroupsView = document.getElementById("wc-projected-groups");
const homeUpcomingList = document.getElementById("home-upcoming-list");
const homeDateToggle = document.getElementById("home-date-toggle");
const homeDatePopover = document.getElementById("home-date-popover");
const homeStartDate = document.getElementById("home-start-date");
const homeEndDate = document.getElementById("home-end-date");
const homeApplyDates = document.getElementById("home-apply-dates");

function renderHomeWcWinnerOdds(data) {
    if (!wcWinnerOddsView) return;
    const winnerProbabilities = data?.simulations?.winner_probabilities || {};
    const entries = Object.entries(winnerProbabilities)
        .filter(([, probability]) => Number(probability) > 0)
        .sort((left, right) => Number(right[1]) - Number(left[1]))
        .slice(0, 9);
    if (!entries.length) {
        wcWinnerOddsView.innerHTML = "<p class=\"muted-placeholder\">No World Cup projection data available.</p>";
        return;
    }
    wcWinnerOddsView.innerHTML = entries.map(([team, probability]) => `
        <div class="world-cup-odds-card">
            <span>${escapeHtmlText(team)}</span>
            <strong>${formatPct(probability)}</strong>
        </div>
    `).join("");
}

function renderHomeWcProjectedGroups(data) {
    if (!wcProjectedGroupsView) return;
    const groupTables = Array.isArray(data?.group_tables) ? data.group_tables : [];
    if (!groupTables.length) {
        wcProjectedGroupsView.innerHTML = "<p class=\"muted-placeholder\">No World Cup projection data available.</p>";
        return;
    }
    wcProjectedGroupsView.innerHTML = groupTables.map((group) => {
        const teams = group.teams || [];
        return `
        <div class="world-cup-table-card">
            <h5>Group ${escapeHtmlText(group.group)}</h5>
            <div class="table-scroll">
                <table class="standings-table world-cup-table">
                    <thead>
                        <tr>
                            <th>Team</th><th>P</th><th>W</th><th>D</th><th>L</th><th>GD</th><th>Pts</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${teams.map((team) => `
                        <tr class="${team.qualified ? "qualified-row" : ""}">
                            <td>${escapeHtmlText(team.team)}</td>
                            <td>${formatCell(team.P)}</td>
                            <td>${formatCell(team.W)}</td>
                            <td>${formatCell(team.D)}</td>
                            <td>${formatCell(team.L)}</td>
                            <td>${formatCell(team.GD)}</td>
                            <td><strong>${formatCell(team.Pts)}</strong></td>
                        </tr>
                        `).join("")}
                    </tbody>
                </table>
            </div>
        </div>
        `;
    }).join("");
}

async function loadHomeWorldCup() {
    if (!wcWinnerOddsView && !wcProjectedGroupsView) return;
    try {
        const response = await fetch("/api/world-cup");
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) {
            if (wcWinnerOddsView) wcWinnerOddsView.innerHTML = "<p class=\"muted-placeholder\">World Cup projection not available yet.</p>";
            if (wcProjectedGroupsView) wcProjectedGroupsView.innerHTML = "<p class=\"muted-placeholder\">World Cup projection not available yet.</p>";
            return;
        }
        renderHomeWcWinnerOdds(data);
        renderHomeWcProjectedGroups(data);
    } catch (_error) {
        if (wcWinnerOddsView) wcWinnerOddsView.innerHTML = "<p class=\"muted-placeholder\">Failed to load World Cup data.</p>";
        if (wcProjectedGroupsView) wcProjectedGroupsView.innerHTML = "<p class=\"muted-placeholder\">Failed to load World Cup data.</p>";
    }
}

function isoToday() {
    // Use the browser's local day to match the date picker and default home view.
    const now = new Date();
    const offsetMs = now.getTimezoneOffset() * 60000;
    return new Date(now.getTime() - offsetMs).toISOString().slice(0, 10);
}

function formatDateButton(start, end) {
    // Keep the compact button readable for same-day and range selections.
    const today = isoToday();
    if (start === today && end === today) return "Today";
    const dateOptions = { month: "short", day: "numeric" };
    const startLabel = new Date(`${start}T12:00:00`).toLocaleDateString([], dateOptions);
    const endLabel = new Date(`${end}T12:00:00`).toLocaleDateString([], dateOptions);
    return start === end ? startLabel : `${startLabel} - ${endLabel}`;
}

function renderHomeUpcoming(data) {
    if (!homeUpcomingList) return;
    const rows = Array.isArray(data?.rows) ? data.rows : [];
    if (!rows.length) {
        homeUpcomingList.innerHTML = "<p class=\"muted-placeholder\">No matches found for this date range.</p>";
        return;
    }
    // Reuse the Upcoming Matches tab renderer so the card format stays identical.
    renderUpcoming(homeUpcomingList, rows, "", { includePast: true, groupByLeague: true });
}

async function loadHomeUpcoming(start = isoToday(), end = start) {
    if (!homeUpcomingList) return;
    homeUpcomingList.innerHTML = "<p class=\"muted-placeholder\">Loading matches...</p>";
    try {
        const params = new URLSearchParams({ start, end });
        const response = await fetch(`/api/home/upcoming?${params.toString()}`);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data?.ok) {
            homeUpcomingList.innerHTML = "<p class=\"muted-placeholder\">Failed to load upcoming matches.</p>";
            return;
        }
        if (homeStartDate && homeEndDate) {
            homeStartDate.min = data.window_start || "";
            homeStartDate.max = data.window_end || "";
            homeEndDate.min = data.window_start || "";
            homeEndDate.max = data.window_end || "";
            homeStartDate.value = data.start_date || start;
            homeEndDate.value = data.end_date || end;
        }
        if (homeDateToggle) {
            homeDateToggle.textContent = formatDateButton(data.start_date || start, data.end_date || end);
        }
        renderHomeUpcoming(data);
    } catch (_error) {
        homeUpcomingList.innerHTML = "<p class=\"muted-placeholder\">Failed to load upcoming matches.</p>";
    }
}

function setupHomeDatePicker() {
    if (!homeDateToggle || !homeDatePopover || !homeApplyDates) return;
    const today = isoToday();
    if (homeStartDate) homeStartDate.value = today;
    if (homeEndDate) homeEndDate.value = today;

    // Toggle the compact popover that holds both date inputs.
    homeDateToggle.addEventListener("click", () => {
        const isHidden = homeDatePopover.classList.toggle("hidden");
        homeDateToggle.setAttribute("aria-expanded", String(!isHidden));
    });

    homeApplyDates.addEventListener("click", () => {
        const start = homeStartDate?.value || today;
        const end = homeEndDate?.value || start;
        homeDatePopover.classList.add("hidden");
        homeDateToggle.setAttribute("aria-expanded", "false");
        loadHomeUpcoming(start, end);
    });
}

function setupHomeUpcomingClicks() {
    if (!homeUpcomingList) return;
    // Match the Upcoming tab behavior by opening selected games in head-to-head.
    homeUpcomingList.addEventListener("click", (event) => {
        const button = event.target.closest(".match-toggle");
        if (!button || typeof openMatchupInH2H !== "function") return;
        openMatchupInH2H(button.getAttribute("data-home-team"), button.getAttribute("data-away-team"));
    });
}

function formatPct(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "0%";
    return `${number.toFixed(1)}%`;
}

function formatCell(value) {
    if (value === null || value === undefined || value === "") return "-";
    return escapeHtmlText(String(value));
}

function escapeHtmlText(value) {
    const map = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;",
    };
    return String(value).replace(/[&<>"']/g, (character) => map[character]);
}

// fetch and render winners for the single home-page block (no dataset dropdown)
if (winnerView) {
    loadHomeWinners();
}

loadHomeWorldCup();
setupHomeDatePicker();
setupHomeUpcomingClicks();
loadHomeUpcoming();
