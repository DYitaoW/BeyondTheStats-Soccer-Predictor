// Home page: upcoming cards + league sidebar via /api/upcoming and /api/league-leaders.
// Do not call deprecated /api/home/* endpoints.

const homeLeagueSidebar = document.getElementById("home-league-sidebar");

function formatLeagueLabel(league) {
    const parts = String(league || "").split("/");
    if (parts.length < 2) return { country: "", name: league };
    return { country: parts[0], name: parts.slice(1).join("/") };
}

function leagueTableUrl(dataset, league) {
    const params = new URLSearchParams({ dataset, league });
    return `/league-tables?${params.toString()}`;
}

function formatSidebarWinPct(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) return "0%";
    if (typeof asPct === "function") return asPct(number);
    return `${number.toFixed(1)}%`;
}

function datasetForCompetition(competition) {
    const name = String(competition || "");
    if (name.startsWith("United States/MLS") || name === "Mexico/Liga MX") return "mls";
    if (typeof EXTRA_DATASET_COMPETITIONS !== "undefined") {
        // optional global; fall through to heuristic
    }
    const extraPrefixes = [
        "Argentina/", "Brazil/", "Japan/", "China/", "Australia/",
        "South Korea/", "Chile/", "Colombia/", "Peru/", "Uruguay/",
    ];
    if (extraPrefixes.some((prefix) => name.startsWith(prefix))) return "extra";
    return "global";
}

function renderHomeLeagueSidebar(entries) {
    if (!homeLeagueSidebar) return;
    if (!entries.length) {
        homeLeagueSidebar.innerHTML = "<p class=\"muted-placeholder\">No league data available.</p>";
        return;
    }

    homeLeagueSidebar.innerHTML = entries.map((entry) => {
        const label = formatLeagueLabel(entry.league);
        const href = leagueTableUrl(entry.dataset, entry.league);
        const winPct = formatSidebarWinPct(entry.win_pct);
        return `
            <a class="home-league-item" href="${escapeHtmlText(href)}">
                <div class="home-league-item-top">
                    <span class="home-league-name">${escapeHtmlText(label.name)}</span>
                    <span class="home-league-pct">${escapeHtmlText(winPct)}</span>
                </div>
                ${label.country ? `<span class="home-league-country">${escapeHtmlText(label.country)}</span>` : ""}
                <span class="home-league-winner">${escapeHtmlText(entry.winner || "N/A")}</span>
            </a>
        `;
    }).join("");
}

async function loadHomeLeagueSidebar() {
    if (!homeLeagueSidebar) return;
    homeLeagueSidebar.innerHTML = "<p class=\"muted-placeholder\">Loading leagues...</p>";
    try {
        const response = await fetch("/api/league-leaders");
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data?.ok) {
            homeLeagueSidebar.innerHTML = "<p class=\"muted-placeholder\">Failed to load leagues.</p>";
            return;
        }
        const entries = (Array.isArray(data.leagues) ? data.leagues : []).map((row) => ({
            dataset: datasetForCompetition(row.competition),
            league: row.competition,
            winner: row.predicted_winner || "N/A",
            win_pct: Number(row.predicted_winner_odds) || 0,
        }));
        renderHomeLeagueSidebar(entries);
    } catch (_error) {
        homeLeagueSidebar.innerHTML = "<p class=\"muted-placeholder\">Failed to load leagues.</p>";
    }
}

const homeUpcomingList = document.getElementById("home-upcoming-list");
const homeDateToggle = document.getElementById("home-date-toggle");
const homeDatePopover = document.getElementById("home-date-popover");
const homeStartDate = document.getElementById("home-start-date");
const homeEndDate = document.getElementById("home-end-date");
const homeApplyDates = document.getElementById("home-apply-dates");

function isoToday() {
    try {
        return new Intl.DateTimeFormat("en-CA", {
            timeZone: "America/New_York",
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
        }).format(new Date());
    } catch (_err) {
        const now = new Date();
        const offsetMs = now.getTimezoneOffset() * 60000;
        return new Date(now.getTime() - offsetMs).toISOString().slice(0, 10);
    }
}

function formatDateButton(start, end) {
    const today = isoToday();
    if (start === today && end === today) return "Today";
    const dateOptions = { month: "short", day: "numeric" };
    const startLabel = new Date(`${start}T12:00:00`).toLocaleDateString([], dateOptions);
    const endLabel = new Date(`${end}T12:00:00`).toLocaleDateString([], dateOptions);
    return start === end ? startLabel : `${startLabel} - ${endLabel}`;
}

function rowMatchDateIso(row) {
    if (typeof rowDateIso === "function") {
        const value = rowDateIso(row);
        if (value) return value;
    }
    const raw = row?.match_date_iso || row?.match_date || "";
    const text = String(raw).trim();
    if (/^\d{4}-\d{2}-\d{2}/.test(text)) return text.slice(0, 10);
    return "";
}

function renderHomeUpcoming(rows) {
    if (!homeUpcomingList) return;
    if (!rows.length) {
        homeUpcomingList.innerHTML = "<p class=\"muted-placeholder\">No matches found for this date range.</p>";
        return;
    }
    renderUpcoming(homeUpcomingList, rows, "", { includePast: true, groupByLeague: true });
}

function filterRowsByDateRange(rows, start, end) {
    return (rows || []).filter((row) => {
        const dateIso = rowMatchDateIso(row);
        if (!dateIso) return false;
        return dateIso >= start && dateIso <= end;
    });
}

let _homeRefreshId = null;
let _homeUpcomingCache = null;

async function fetchUpcomingRowsForHome() {
    // Prefer the aggregated upcoming feed (all sources). Avoid deprecated /api/home/*.
    const response = await fetch("/api/upcoming/global");
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data?.ok) {
        const err = new Error(data?.error || `Upcoming API failed (${response.status})`);
        err.status = response.status;
        throw err;
    }
    return Array.isArray(data.rows) ? data.rows : [];
}

async function loadHomeUpcoming(start = isoToday(), end = start) {
    if (!homeUpcomingList) return;
    homeUpcomingList.innerHTML = "<p class=\"muted-placeholder\">Loading matches...</p>";
    try {
        if (!_homeUpcomingCache) {
            _homeUpcomingCache = await fetchUpcomingRowsForHome();
        }
        const rows = filterRowsByDateRange(_homeUpcomingCache, start, end);
        if (homeStartDate && homeEndDate) {
            homeStartDate.value = start;
            homeEndDate.value = end;
        }
        if (homeDateToggle) {
            homeDateToggle.textContent = formatDateButton(start, end);
        }
        renderHomeUpcoming(rows);
        if (_homeRefreshId) clearInterval(_homeRefreshId);
        _homeRefreshId = setInterval(async () => {
            if (document.hidden) return;
            try {
                _homeUpcomingCache = await fetchUpcomingRowsForHome();
                renderHomeUpcoming(filterRowsByDateRange(_homeUpcomingCache, start, end));
            } catch (_refreshErr) {
                // Keep the last successful render on refresh failure.
            }
        }, 120000);
    } catch (_error) {
        homeUpcomingList.innerHTML = "<p class=\"muted-placeholder\">Failed to load upcoming matches.</p>";
    }
}

function setupHomeDatePicker() {
    if (!homeDateToggle || !homeDatePopover || !homeApplyDates) return;
    const today = isoToday();
    if (homeStartDate) homeStartDate.value = today;
    if (homeEndDate) homeEndDate.value = today;

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
    homeUpcomingList.addEventListener("click", (event) => {
        const button = event.target.closest(".match-toggle");
        if (!button || typeof openMatchupInH2H !== "function") return;
        openMatchupInH2H(button.getAttribute("data-home-team"), button.getAttribute("data-away-team"));
    });
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

loadHomeLeagueSidebar();
setupHomeDatePicker();
setupHomeUpcomingClicks();
loadHomeUpcoming();
