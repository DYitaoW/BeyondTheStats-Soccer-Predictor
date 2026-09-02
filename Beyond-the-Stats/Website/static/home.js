// Home page: upcoming cards + league sidebar via /api/upcoming and /api/league-leaders.
// Do not call deprecated /api/home/* endpoints.

const homeLeagueSidebar = document.getElementById("home-league-sidebar");

function formatLeagueLabel(league) {
    const parts = String(league || "").split("/");
    if (parts.length < 2) return { country: "", name: league };
    return { country: parts[0], name: parts.slice(1).join("/") };
}

function leagueTableUrl(league) {
    const params = new URLSearchParams({ competition: league });
    return `/leagues?${params.toString()}`;
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
        const href = leagueTableUrl(entry.league);
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
const homeDateHeading = document.getElementById("home-upcoming-date-heading");
const homeApplyDates = document.getElementById("home-apply-dates");
const homeCalGrid = document.getElementById("home-cal-grid");
const homeCalMonthLabel = document.getElementById("home-cal-month-label");
const homeCalPrev = document.getElementById("home-cal-prev");
const homeCalNext = document.getElementById("home-cal-next");
const homeCalToday = document.getElementById("home-cal-today");
const homeCalHint = document.getElementById("home-cal-hint");

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

function formatLongDate(isoDate) {
    if (typeof formatMatchDayLabel === "function") {
        return formatMatchDayLabel(isoDate);
    }
    try {
        const dt = new Date(`${isoDate}T12:00:00`);
        return new Intl.DateTimeFormat("en-US", {
            weekday: "long",
            month: "long",
            day: "numeric",
            year: "numeric",
        }).format(dt);
    } catch (_err) {
        return isoDate;
    }
}

function normalizeDateRange(start, end) {
    const a = String(start || "").trim();
    const b = String(end || a).trim();
    if (!a) return { start: b, end: b };
    if (!b) return { start: a, end: a };
    return a <= b ? { start: a, end: b } : { start: b, end: a };
}

function formatDateButton(start, end) {
    const range = normalizeDateRange(start, end);
    const today = isoToday();
    if (range.start === today && range.end === today) return "Today";
    if (range.start === range.end) {
        return new Date(`${range.start}T12:00:00`).toLocaleDateString([], { month: "short", day: "numeric" });
    }
    const startLabel = new Date(`${range.start}T12:00:00`).toLocaleDateString([], { month: "short", day: "numeric" });
    const endLabel = new Date(`${range.end}T12:00:00`).toLocaleDateString([], { month: "short", day: "numeric" });
    return `${startLabel} – ${endLabel}`;
}

function updateHomeDateHeading(start, end) {
    if (!homeDateHeading) return;
    const range = normalizeDateRange(start, end);
    if (range.start === range.end) {
        homeDateHeading.textContent = formatLongDate(range.start);
        return;
    }
    homeDateHeading.textContent = `${formatLongDate(range.start)} – ${formatLongDate(range.end)}`;
}

let homeSelectedStart = isoToday();
let homeSelectedEnd = isoToday();
let homeCalView = new Date(`${homeSelectedStart}T12:00:00`);
let homeCalAnchor = homeSelectedStart;
let homeCalFocus = homeSelectedEnd;
let homeCalClickCount = 0;

function isoFromDate(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
}

function isDateInRange(iso, start, end) {
    return iso >= start && iso <= end;
}

function updateCalendarHint() {
    if (!homeCalHint) return;
    const range = normalizeDateRange(homeCalAnchor, homeCalFocus);
    if (!homeCalAnchor) {
        homeCalHint.textContent = "Select one date, or two dates for a range.";
        return;
    }
    if (range.start === range.end) {
        homeCalHint.textContent = `Selected: ${formatLongDate(range.start)}`;
        return;
    }
    homeCalHint.textContent = `Selected: ${formatLongDate(range.start)} – ${formatLongDate(range.end)}`;
}

function renderHomeCalendar() {
    if (!homeCalGrid || !homeCalMonthLabel) return;
    const year = homeCalView.getFullYear();
    const month = homeCalView.getMonth();
    homeCalMonthLabel.textContent = new Intl.DateTimeFormat("en-US", {
        month: "long",
        year: "numeric",
    }).format(homeCalView);

    const range = normalizeDateRange(homeCalAnchor, homeCalFocus);
    const firstOfMonth = new Date(year, month, 1);
    const startOffset = firstOfMonth.getDay();
    const gridStart = new Date(year, month, 1 - startOffset);

    homeCalGrid.innerHTML = "";
    for (let i = 0; i < 42; i += 1) {
        const cellDate = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + i);
        const iso = isoFromDate(cellDate);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "home-calendar-day";
        button.textContent = String(cellDate.getDate());
        button.dataset.date = iso;
        button.setAttribute("aria-label", formatLongDate(iso));

        if (cellDate.getMonth() !== month) {
            button.classList.add("outside-month");
        }
        if (iso === isoToday()) {
            button.classList.add("today");
        }
        if (homeCalAnchor && range.start === range.end && iso === range.start) {
            button.classList.add("selected-single");
        } else if (homeCalAnchor && isDateInRange(iso, range.start, range.end)) {
            button.classList.add("in-range");
            if (iso === range.start) button.classList.add("range-start");
            if (iso === range.end) button.classList.add("range-end");
        }

        button.addEventListener("click", () => {
            if (homeCalClickCount === 0 || homeCalClickCount >= 2) {
                homeCalAnchor = iso;
                homeCalFocus = iso;
                homeCalClickCount = 1;
            } else {
                homeCalFocus = iso;
                homeCalClickCount = 2;
            }
            renderHomeCalendar();
            updateCalendarHint();
        });

        homeCalGrid.appendChild(button);
    }
    updateCalendarHint();
}

function syncCalendarSelection(start, end) {
    const range = normalizeDateRange(start, end);
    homeCalAnchor = range.start;
    homeCalFocus = range.end;
    homeCalView = new Date(`${range.start}T12:00:00`);
    homeCalClickCount = range.start === range.end ? 1 : 2;
    renderHomeCalendar();
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

function renderHomeUpcoming(rows, start, end) {
    if (!homeUpcomingList) return;
    if (!rows.length) {
        homeUpcomingList.innerHTML = "<p class=\"muted-placeholder\">No matches found for this date range.</p>";
        updateHomeDateHeading(start, end);
        return;
    }
    const range = normalizeDateRange(start, end);
    const singleDay = range.start === range.end;
    renderUpcoming(homeUpcomingList, rows, "", {
        includePast: true,
        groupByDateThenLeague: true,
        hideDayHeaders: singleDay,
    });
    updateHomeDateHeading(range.start, range.end);
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
    // Prefer the aggregated upcoming feed (all sources) limited to the fast
    // 4-week window (past 14 days + next 21 days) so the home page loads less data.
    const response = await fetch("/api/upcoming/global?window=4week");
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
    const range = normalizeDateRange(start, end);
    homeSelectedStart = range.start;
    homeSelectedEnd = range.end;
    try {
        if (!_homeUpcomingCache) {
            _homeUpcomingCache = await fetchUpcomingRowsForHome();
        }
        const rows = filterRowsByDateRange(_homeUpcomingCache, range.start, range.end);
        if (homeDateToggle) {
            homeDateToggle.textContent = formatDateButton(range.start, range.end);
        }
        renderHomeUpcoming(rows, range.start, range.end);
        if (_homeRefreshId) clearInterval(_homeRefreshId);
        _homeRefreshId = setInterval(async () => {
            if (document.hidden) return;
            try {
                _homeUpcomingCache = await fetchUpcomingRowsForHome();
                renderHomeUpcoming(
                    filterRowsByDateRange(_homeUpcomingCache, homeSelectedStart, homeSelectedEnd),
                    homeSelectedStart,
                    homeSelectedEnd,
                );
            } catch (_refreshErr) {
                // Keep the last successful render on refresh failure.
            }
        }, 120000);
    } catch (_error) {
        homeUpcomingList.innerHTML = "<p class=\"muted-placeholder\">Failed to load upcoming matches.</p>";
        updateHomeDateHeading(range.start, range.end);
    }
}

function setupHomeDatePicker() {
    if (!homeDateToggle || !homeDatePopover || !homeApplyDates) return;
    const today = isoToday();
    syncCalendarSelection(today, today);

    homeDateToggle.addEventListener("click", (event) => {
        event.stopPropagation();
        const isHidden = homeDatePopover.classList.toggle("hidden");
        homeDateToggle.setAttribute("aria-expanded", String(!isHidden));
        if (!isHidden) {
            syncCalendarSelection(homeSelectedStart, homeSelectedEnd);
        }
    });

    homeCalPrev?.addEventListener("click", (event) => {
        event.stopPropagation();
        homeCalView = new Date(homeCalView.getFullYear(), homeCalView.getMonth() - 1, 1);
        renderHomeCalendar();
    });

    homeCalNext?.addEventListener("click", (event) => {
        event.stopPropagation();
        homeCalView = new Date(homeCalView.getFullYear(), homeCalView.getMonth() + 1, 1);
        renderHomeCalendar();
    });

    homeCalToday?.addEventListener("click", (event) => {
        event.stopPropagation();
        syncCalendarSelection(today, today);
    });

    homeApplyDates.addEventListener("click", (event) => {
        event.stopPropagation();
        const range = normalizeDateRange(homeCalAnchor, homeCalFocus);
        homeDatePopover.classList.add("hidden");
        homeDateToggle.setAttribute("aria-expanded", "false");
        loadHomeUpcoming(range.start, range.end);
    });

    document.addEventListener("click", (event) => {
        if (homeDatePopover.classList.contains("hidden")) return;
        if (event.target.closest(".date-range-picker")) return;
        homeDatePopover.classList.add("hidden");
        homeDateToggle.setAttribute("aria-expanded", "false");
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
