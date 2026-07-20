// preloadHomeData is invoked from shared.js so upcoming cards and summary stats render on every page

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
        const response = await fetch("/api/home/league-sidebar");
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data?.ok) {
            homeLeagueSidebar.innerHTML = "<p class=\"muted-placeholder\">Failed to load leagues.</p>";
            return;
        }
        renderHomeLeagueSidebar(Array.isArray(data.leagues) ? data.leagues : []);
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
    const now = new Date();
    const offsetMs = now.getTimezoneOffset() * 60000;
    return new Date(now.getTime() - offsetMs).toISOString().slice(0, 10);
}

function formatDateButton(start, end) {
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
    renderUpcoming(homeUpcomingList, rows, "", { includePast: true, groupByLeague: true });
}

let _homeRefreshId = null;

async function loadHomeUpcoming(start = isoToday(), end = start) {
    if (!homeUpcomingList) return;
    homeUpcomingList.innerHTML = "<p class=\"muted-placeholder\">Loading matches...</p>";
    try {
        const params = new URLSearchParams({ start, end });
        const response = await fetch(`/api/home/upcoming?${params.toString()}`);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data?.ok) {
            homeUpcomingList.innerHTML = "<p class=\"muted-placeholder\">No upcoming matches available right now.</p>";
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
        if (_homeRefreshId) clearInterval(_homeRefreshId);
        _homeRefreshId = setInterval(async () => {
            if (document.hidden) return;
            const refreshResp = await fetch(`/api/home/upcoming?${params.toString()}`);
            const refreshData = await refreshResp.json().catch(() => ({}));
            if (refreshResp.ok && refreshData?.ok) {
                renderHomeUpcoming(refreshData);
            }
        }, 120000);
    } catch (_error) {
        homeUpcomingList.innerHTML = "<p class=\"muted-placeholder\">No upcoming matches available right now.</p>";
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

loadHomeLeagueSidebar();
setupHomeDatePicker();
setupHomeUpcomingClicks();
loadHomeUpcoming();
