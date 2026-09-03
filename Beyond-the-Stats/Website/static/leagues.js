// Leagues hub (tile grid) + competition detail views.

const hubSection = document.getElementById("leagues-hub");
const detailSection = document.getElementById("league-detail");
const hubLoading = document.getElementById("leagues-hub-loading");
const hubLeaguesSection = document.getElementById("leagues-hub-leagues");
const hubCupsSection = document.getElementById("leagues-hub-cups");
const gridLeagues = document.getElementById("leagues-tile-grid-leagues");
const gridCups = document.getElementById("leagues-tile-grid-cups");

const CUP_COMPETITIONS = new Set([
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
    "England/FA Cup",
    "England/League Cup",
    "North America/Leagues Cup",
    "Germany/DFB-Pokal",
    "Italy/Coppa Italia",
    "Spain/Copa del Rey",
    "France/Coupe de France",
    "United States/US Open Cup",
    "International/World Cup",
]);

function formatLeagueLabel(competition) {
    const displayName = typeof competitionDisplayName === "function"
        ? competitionDisplayName(competition)
        : String(competition || "").split("/").slice(1).join("/") || competition;
    const parts = String(competition || "").split("/");
    if (parts.length < 2) {
        return { country: "", name: displayName };
    }
    return {
        country: parts[0],
        name: displayName,
    };
}

async function fetchLeagueLeadersJson() {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 30000);
    try {
        const response = await fetch("/api/league-leaders", { signal: controller.signal });
        const data = await response.json().catch(() => ({}));
        return { response, data };
    } finally {
        clearTimeout(timeoutId);
    }
}

function leagueDetailUrl(competition) {
    const params = new URLSearchParams({ competition });
    return `/leagues?${params.toString()}`;
}

function tileVisual(competition) {
    const label = formatLeagueLabel(competition);
    const seed = [...String(competition)].reduce((sum, ch) => sum + ch.charCodeAt(0), 0);
    const tones = ["#3f4a55", "#465460", "#4d5c68", "#556371", "#5a6672", "#4a5560"];
    const tone = tones[seed % tones.length];
    const initials = (label.name || competition)
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 2)
        .map((word) => word[0])
        .join("")
        .toUpperCase() || "?";
    return {
        initials,
        country: label.country,
        name: label.name,
        style: `background: ${tone};`,
    };
}

function escapeText(value) {
    if (typeof escapeHtml === "function") return escapeHtml(value);
    return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function formatWinPct(value) {
    if (typeof asPct === "function") return asPct(value);
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return "—";
    return `${n.toFixed(1)}%`;
}

function renderLeagueTile(entry, isCup) {
    const competition = entry.competition;
    const visual = tileVisual(competition);
    const href = leagueDetailUrl(competition);
    const winner = entry.predicted_winner || entry.current_leader || "—";
    const odds = entry.predicted_winner_odds;
    const badge = isCup ? '<span class="league-tile-badge">Cup</span>' : "";
    return `
        <a class="league-tile" href="${escapeText(href)}" role="listitem">
            <div class="league-tile-art" style="${visual.style}">
                <span class="league-tile-initials">${escapeText(visual.initials)}</span>
            </div>
            <div class="league-tile-body">
                ${badge}
                <span class="league-tile-name">${escapeText(visual.name)}</span>
                <span class="league-tile-meta">${escapeText(winner)}${odds != null ? ` · ${escapeText(formatWinPct(odds))}` : ""}</span>
            </div>
        </a>
    `;
}

function dedupeCompetitionEntries(entries) {
    const byKey = new Map();
    for (const entry of entries || []) {
        const comp = String(entry?.competition || "").trim();
        if (!comp) continue;
        const key = comp.toLowerCase();
        if (!byKey.has(key)) {
            byKey.set(key, { ...entry, competition: comp });
        }
    }
    const values = [...byKey.values()];
    if (typeof sortCompetitionsByPreferredOrder === "function") {
        return sortCompetitionsByPreferredOrder(values, (entry) => entry.competition);
    }
    return values.sort((a, b) => a.competition.localeCompare(b.competition));
}

async function loadLeaguesHub() {
    if (!hubLoading) return;
    try {
        const { response, data } = await fetchLeagueLeadersJson();
        if (!response.ok || !data?.ok) {
            hubLoading.textContent = data?.error || "Failed to load leagues.";
            return;
        }
        const leagues = dedupeCompetitionEntries(data.leagues || [])
            .filter((entry) => typeof competitionHasPredictions !== "function" || competitionHasPredictions(entry));
        const cups = dedupeCompetitionEntries(data.cups || [])
            .filter((entry) => typeof competitionHasPredictions !== "function" || competitionHasPredictions(entry));

        if (gridLeagues) {
            gridLeagues.innerHTML = leagues.length
                ? leagues.map((entry) => renderLeagueTile(entry, false)).join("")
                : "";
            hubLeaguesSection?.classList.toggle("hidden", !leagues.length);
        }
        if (gridCups) {
            gridCups.innerHTML = cups.length
                ? cups.map((entry) => renderLeagueTile(entry, true)).join("")
                : "";
            hubCupsSection?.classList.toggle("hidden", !cups.length);
        }

        if (!leagues.length && !cups.length) {
            hubLoading.textContent = "No competitions available yet.";
            return;
        }
        hubLoading.classList.add("hidden");
    } catch (err) {
        hubLoading.classList.remove("hidden");
        hubLoading.textContent = err?.name === "AbortError"
            ? "Loading timed out. Please refresh."
            : "Failed to load leagues.";
    }
}

function renderRealStandingsGroups(standings) {
    if (!standings || !Array.isArray(standings.groups) || !standings.groups.length) {
        return "<p class=\"muted-placeholder\">No real standings available yet.</p>";
    }
    let html = "";
    for (const group of standings.groups) {
        const entries = group.entries || [];
        if (!entries.length) continue;
        html += `<h4>${escapeText(group.name || "Table")}</h4>`;
        html += `
            <table class="league-table">
                <thead>
                    <tr><th>#</th><th>Team</th><th>P</th><th>W</th><th>D</th><th>L</th><th>GF</th><th>GA</th><th>GD</th><th>Pts</th></tr>
                </thead>
                <tbody>
        `;
        for (const row of entries) {
            html += `
                <tr>
                    <td>${row.rank ?? row.position ?? ""}</td>
                    <td>${escapeText(row.team)}</td>
                    <td>${row.P ?? row.played ?? 0}</td>
                    <td>${row.W ?? row.won ?? 0}</td>
                    <td>${row.D ?? row.drawn ?? 0}</td>
                    <td>${row.L ?? row.lost ?? 0}</td>
                    <td>${row.GF ?? row.goals_for ?? 0}</td>
                    <td>${row.GA ?? row.goals_against ?? 0}</td>
                    <td>${row.GD ?? row.goal_difference ?? 0}</td>
                    <td><strong>${row.Pts ?? row.points ?? 0}</strong></td>
                </tr>
            `;
        }
        html += "</tbody></table>";
    }
    return html || "<p class=\"muted-placeholder\">No real standings available yet.</p>";
}

function renderPredictedFromPayload(payload, competition) {
    const rows = payload?.predicted?.table || payload?.predicted_table || [];
    if (!rows.length) {
        return "<p class=\"muted-placeholder\">No projected table data available.</p>";
    }
    if (typeof renderLeagueTableRows === "function") {
        return renderLeagueTableRows(rows, competition);
    }
    return "<p class=\"muted-placeholder\">No projected table data available.</p>";
}

function renderPositionOddsFromPayload(payload, competition) {
    const rows = payload?.predicted?.table || payload?.predicted_table || [];
    if (!rows.length) {
        return "<p class=\"muted-placeholder\">No position odds available.</p>";
    }
    if (typeof renderPositionOddsRows === "function") {
        return renderPositionOddsRows(rows, competition);
    }
    return "<p class=\"muted-placeholder\">No position odds available.</p>";
}

function renderUpcomingForCompetition(fixtures, competition) {
    const rows = (fixtures || []).filter((row) => String(row.competition || "").trim() === competition);
    const container = document.createElement("div");
    if (!rows.length) {
        return "<p class=\"muted-placeholder\">No upcoming fixtures for this competition.</p>";
    }
    if (typeof renderUpcoming === "function") {
        renderUpcoming(container, rows, competition, { includePast: false, groupByLeague: false });
        return container.innerHTML;
    }
    return "<p class=\"muted-placeholder\">No upcoming fixtures for this competition.</p>";
}

function renderBracketFromPayload(payload, competition) {
    const bracket = payload?.bracket || {};
    const projected = bracket?.projected || bracket?.knockout || null;
    if (projected && typeof projected === "object") {
        return `<pre class="bracket-json-fallback">${escapeText(JSON.stringify(projected, null, 2))}</pre>`;
    }
    return "<p class=\"muted-placeholder\">No bracket projection available for this competition.</p>";
}

function setDetailTab(view) {
    document.querySelectorAll(".league-detail-tab").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.view === view);
    });
    const panels = {
        predicted: document.getElementById("league-detail-predicted"),
        real: document.getElementById("league-detail-real"),
        "position-odds": document.getElementById("league-detail-position-odds"),
        upcoming: document.getElementById("league-detail-upcoming"),
        bracket: document.getElementById("league-detail-bracket"),
    };
    Object.entries(panels).forEach(([key, panel]) => {
        if (panel) panel.classList.toggle("hidden", key !== view);
    });
}

function renderDetailLeaders(entry, competition) {
    const leaders = document.getElementById("league-detail-leaders");
    if (!leaders) return;
    const predicted = entry?.predicted_winner || "—";
    const predictedOdds = entry?.predicted_winner_odds;
    const realLeader = entry?.current_leader || "—";
    leaders.innerHTML = `
        <div class="league-leader-chip">
            <span class="league-leader-label">Projected winner</span>
            <strong>${escapeText(predicted)}</strong>
            ${predictedOdds != null ? `<span class="league-leader-odds">${escapeText(formatWinPct(predictedOdds))}</span>` : ""}
        </div>
        <div class="league-leader-chip">
            <span class="league-leader-label">Current leader</span>
            <strong>${escapeText(realLeader)}</strong>
        </div>
    `;
}

async function loadLeagueDetail(competition) {
    const label = formatLeagueLabel(competition);
    const visual = tileVisual(competition);
    const titleEl = document.getElementById("league-detail-title");
    const countryEl = document.getElementById("league-detail-country");
    const artEl = document.getElementById("league-detail-art");
    const bracketTab = document.getElementById("league-detail-bracket-tab");

    if (titleEl) titleEl.textContent = label.name;
    if (countryEl) countryEl.textContent = label.country;
    if (artEl) {
        artEl.style.cssText = visual.style;
        artEl.textContent = visual.initials;
    }
    if (bracketTab) {
        bracketTab.classList.toggle("hidden", !CUP_COMPETITIONS.has(competition));
    }

    hubSection?.classList.add("hidden");
    detailSection?.classList.remove("hidden");
    setDetailTab("predicted");

    const predictedPanel = document.getElementById("league-detail-predicted");
    const realPanel = document.getElementById("league-detail-real");
    const oddsPanel = document.getElementById("league-detail-position-odds");
    const upcomingPanel = document.getElementById("league-detail-upcoming");
    const bracketPanel = document.getElementById("league-detail-bracket");

    [predictedPanel, realPanel, oddsPanel, upcomingPanel, bracketPanel].forEach((panel) => {
        if (panel) panel.innerHTML = "<p class=\"muted-placeholder\">Loading...</p>";
    });

    try {
        const [dataResp, leadersResp] = await Promise.all([
            fetch(`/api/league-data/${encodeURIComponent(competition)}`),
            fetchLeagueLeadersJson(),
        ]);
        const payload = await dataResp.json().catch(() => ({}));
        const leadersData = leadersResp.data || {};

        if (!dataResp.ok || !payload?.ok) {
            const message = payload?.error || "Failed to load competition data.";
            if (predictedPanel) predictedPanel.innerHTML = `<p class="muted-placeholder">${escapeText(message)}</p>`;
            return;
        }

        let leaderEntry = null;
        if (leadersData?.ok) {
            const all = [...(leadersData.leagues || []), ...(leadersData.cups || [])];
            leaderEntry = all.find((row) => String(row.competition) === competition) || null;
        }
        renderDetailLeaders(leaderEntry, competition);

        if (predictedPanel) {
            predictedPanel.innerHTML = `<h3>Projected Table</h3>${renderPredictedFromPayload(payload, competition)}`;
        }
        if (realPanel) {
            const real = payload.real_table || payload.real?.standings;
            realPanel.innerHTML = `<h3>Real Table</h3>${renderRealStandingsGroups(real)}`;
        }
        if (oddsPanel) {
            oddsPanel.innerHTML = `<h3>Position Odds</h3>${renderPositionOddsFromPayload(payload, competition)}`;
        }
        if (upcomingPanel) {
            upcomingPanel.innerHTML = `<h3>Upcoming Fixtures</h3>${renderUpcomingForCompetition(payload.fixtures, competition)}`;
        }
        if (bracketPanel && CUP_COMPETITIONS.has(competition)) {
            let bracketHtml = "<p class=\"muted-placeholder\">No bracket projection available.</p>";
            try {
                const cupResp = await fetch("/api/league-tables?mode=cups");
                const cupData = await cupResp.json().catch(() => ({}));
                if (cupResp.ok && cupData?.ok && typeof renderCupBracket === "function") {
                    const temp = document.createElement("div");
                    renderCupBracket(cupData, competition, temp);
                    if (temp.innerHTML.trim()) {
                        bracketHtml = temp.innerHTML;
                    }
                }
            } catch (_cupErr) {
                // fall through to placeholder
            }
            bracketPanel.innerHTML = `<h3>Bracket</h3>${bracketHtml}`;
        }
    } catch (_err) {
        if (predictedPanel) {
            predictedPanel.innerHTML = "<p class=\"muted-placeholder\">Failed to load competition data.</p>";
        }
    }
}

function setupDetailTabs() {
    document.querySelectorAll(".league-detail-tab").forEach((btn) => {
        btn.addEventListener("click", () => {
            setDetailTab(btn.dataset.view || "predicted");
        });
    });
}

function getCompetitionFromUrl() {
    const params = new URLSearchParams(window.location.search);
    return params.get("competition") || params.get("league") || "";
}

function initLeaguesPage() {
    setupDetailTabs();
    const competition = getCompetitionFromUrl().trim();
    if (competition) {
        loadLeagueDetail(competition);
    } else {
        loadLeaguesHub();
    }
}

initLeaguesPage();
