// Tournament projection pages: World Cup and other cups share the same renderer.
async function loadWorldCupProjection() {
  const viewEl = document.getElementById("world-cup-view");
  if (!viewEl) return;
  await loadTournamentProjection({
    viewEl,
    competition: "International/World Cup",
    endpoint: "/api/world-cup",
    loadingMessage: "Loading World Cup projection...",
    errorMessage: "Failed to load World Cup projection data.",
  });
}

async function loadTournamentProjection({
  viewEl,
  competition,
  endpoint = null,
  projectedRows = null,
  loadingMessage = "Loading cup projection...",
  errorMessage = "Failed to load cup projection data.",
}) {
  if (!viewEl) return;

  viewEl.innerHTML = `<p class="loading-message">${escapeHtml(loadingMessage)}</p>`;

  try {
    const url = endpoint || `/api/competition-data?competition=${encodeURIComponent(competition)}`;
    const response = await fetch(url);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      viewEl.innerHTML = `<p class="error-message">${escapeHtml(data.error || errorMessage)}</p>`;
      return;
    }
    displayWorldCupData(normalizeTournamentData(data, projectedRows), viewEl);
  } catch (error) {
    console.error(`Error loading tournament data for ${competition}:`, error);
    viewEl.innerHTML = `<p class="error-message">Error loading projection: ${escapeHtml(error.message)}</p>`;
  }
}

function winnerProbabilitiesFor(data) {
  return data.simulations?.winner_probabilities || data.winner_probabilities || {};
}

function normalizeGroupTables(data, projectedRows) {
  if (Array.isArray(data.group_tables) && data.group_tables.length) {
    return data.group_tables;
  }

  if (data.group_tables && typeof data.group_tables === "object" && !Array.isArray(data.group_tables)) {
    return Object.entries(data.group_tables).map(([key, group]) => ({
      group: group.group || key,
      teams: group.teams || [],
    }));
  }

  const tableGroups = data.table?.groups;
  if (Array.isArray(tableGroups) && tableGroups.length) {
    return tableGroups.map((group) => ({
      group: group.name || "Overall",
      teams: (group.entries || []).map((entry) => ({
        team: entry.team,
        P: entry.P,
        W: entry.W,
        D: entry.D,
        L: entry.L,
        GF: entry.GF,
        GA: entry.GA,
        GD: entry.GD,
        Pts: entry.Pts,
        position: entry.rank || entry.position,
        PlayedPred: entry.PlayedPred,
        PlayedReal: entry.PlayedReal,
      })),
    }));
  }

  if (Array.isArray(projectedRows) && projectedRows.length) {
    return [{
      group: "League Phase",
      teams: projectedRows.map((row) => ({
        team: row.team,
        P: row.P,
        W: row.W,
        D: row.D,
        L: row.L,
        GF: row.GF,
        GA: row.GA,
        GD: row.GD,
        Pts: row.Pts,
        position: row.position,
        PlayedPred: row.PlayedPred,
        PlayedReal: row.PlayedReal,
      })),
    }];
  }

  return [];
}

function normalizeTournamentData(data, projectedRows = null) {
  const normalized = { ...data };
  const winnerProbabilities = winnerProbabilitiesFor(data);
  normalized.group_tables = normalizeGroupTables(data, projectedRows);
  normalized.simulations = {
    ...(data.simulations || {}),
    winner_probabilities: winnerProbabilities,
    position_probabilities: data.simulations?.position_probabilities || data.position_probabilities || {},
  };
  if (!normalized.winner_probabilities) {
    normalized.winner_probabilities = winnerProbabilities;
  }
  return normalized;
}

// Main renderer: keeps the page useful even when optional projection fields are absent.
function displayWorldCupData(data, container) {
  const knockout = data.knockout || {};
  const champion = data.champion || getFinalWinner(knockout.final);
  const winnerProbabilities = winnerProbabilitiesFor(data);
  const championProbability = winnerProbabilities[champion];
  let html = "";

  html += renderProjectionSummary(data, champion, championProbability);
  html += renderWinnerOdds(winnerProbabilities);
  html += renderGroupTablesWithToggle(data, container);
  html += renderBestThirdTable(data.third_place_table || data.best_third_place || data.best_third_teams || []);
  html += renderGroupFixtures(data.group_fixtures || []);
  html += renderKnockoutRounds(knockout);

  const competitionLabel = data.competition || "this competition";
  container.innerHTML = html || `<p class="info-message">No projection data is available yet for ${escapeHtml(competitionLabel)}.</p>`;
  wireGroupTablesToggle(container);
  wireGroupFixturesFilter(container);
}

// Summary card: surfaces the fields users care about before the long tables.
function renderProjectionSummary(data, champion, championProbability) {
  const generated = formatDateTime(data.generated_at_utc);
  const championPct = Number.isFinite(Number(championProbability)) ? ` (${formatPercentValue(championProbability)})` : "";
  return `
    <div class="world-cup-summary">
      <div class="world-cup-summary-item">
        <span class="summary-label">Projected Champion</span>
        <strong>${escapeHtml(champion || "TBD")}${escapeHtml(championPct)}</strong>
      </div>
      <div class="world-cup-summary-item">
        <span class="summary-label">Competition</span>
        <strong>${escapeHtml(data.competition || "International/World Cup")}</strong>
      </div>
      <div class="world-cup-summary-item">
        <span class="summary-label">Generated</span>
        <strong>${escapeHtml(generated || "Unknown")}</strong>
      </div>
    </div>
  `;
}

// Winner odds: lists every team with a non-zero title chance from the simulation output.
function renderWinnerOdds(winnerProbabilities) {
  const entries = Object.entries(winnerProbabilities)
    .filter(([, probability]) => Number(probability) > 0)
    .sort((left, right) => Number(right[1]) - Number(left[1]));
  if (!entries.length) return "";

  return `
    <div class="world-cup-section">
      <h4>Winner Odds</h4>
      <div class="world-cup-odds-grid">
        ${entries.map(([team, probability]) => `
          <div class="world-cup-odds-card">
            <span>${escapeHtml(team)}</span>
            <strong>${formatPercentValue(probability)}</strong>
          </div>
        `).join("")}
      </div>
    </div>
  `;
}

// Group standings: renders all inferred World Cup groups in compact tables.
function renderGroupTables(groupTables) {
  if (!groupTables.length) return "";
  return `
    <div class="world-cup-section">
      <h4>Group Tables</h4>
      <div class="world-cup-group-grid">
        ${groupTables.map(renderGroupTable).join("")}
      </div>
    </div>
  `;
}

// Toggle wrapper: shows the single-sim projected tables by default and lets the
// user flip to a per-group position-odds view (chance each team finishes 1st/2nd/3rd/4th).
function renderGroupTablesWithToggle(data, container) {
  const groupTables = data.group_tables || [];
  if (!groupTables.length) return "";

  // Build a per-team position-odds map grouped by group letter for the toggle.
  const positionProbabilities = data.simulations?.position_probabilities || {};
  const teamToGroup = {};
  for (const group of groupTables) {
    for (const team of (group.teams || [])) {
      if (team && team.team) teamToGroup[team.team] = group.group;
    }
  }
  const positionOddsByGroup = groupTables.map((group) => {
    const teams = (group.teams || []).map((team) => {
      const probs = positionProbabilities[team.team] || {};
      return {
        team: team.team,
        positions: {
          1: Number(probs.group_position_1) || 0,
          2: Number(probs.group_position_2) || 0,
          3: Number(probs.group_position_3) || 0,
          4: Number(probs.group_position_4) || 0,
        },
      };
    });
    // Order rows by most-likely finishing position: highest P(1st) first, then P(2nd), etc.
    teams.sort((a, b) => {
      if (b.positions[1] !== a.positions[1]) return b.positions[1] - a.positions[1];
      if (b.positions[2] !== a.positions[2]) return b.positions[2] - a.positions[2];
      if (b.positions[3] !== a.positions[3]) return b.positions[3] - a.positions[3];
      return b.positions[4] - a.positions[4];
    });
    return { group: group.group, teams };
  });

  // Stash the alternate view on the data so the click handler can render it without re-deriving.
  data.__positionOddsByGroup = positionOddsByGroup;
  data.__teamToGroup = teamToGroup;

  return `
    <div class="world-cup-section" data-group-tables-section>
      <div class="world-cup-section-header">
        <h4 id="group-tables-heading">Projected Group Tables</h4>
        <button id="group-tables-toggle" type="button" class="tab-btn active" data-view="tables">
          Show Position Odds
        </button>
      </div>
      <div class="world-cup-group-grid world-cup-groups-2x6" data-group-tables-view="tables">
        ${groupTables.map(renderGroupTable).join("")}
      </div>
      <div class="world-cup-group-grid world-cup-groups-2x6" data-group-tables-view="odds" style="display: none;">
        ${positionOddsByGroup.map(renderPositionOddsTable).join("")}
      </div>
    </div>
  `;
}

// Per-team position-odds card: 4 columns (1st / 2nd / 3rd / 4th) with a probability bar.
function renderPositionOddsTable(groupEntry) {
  return `
    <div class="world-cup-table-card">
      <h5>Group ${escapeHtml(groupEntry.group)}</h5>
      <div class="table-scroll">
        <table class="standings-table world-cup-table position-odds-table">
          <thead>
            <tr>
              <th>Team</th><th>1st</th><th>2nd</th><th>3rd</th><th>4th</th>
            </tr>
          </thead>
          <tbody>
            ${groupEntry.teams.map((team) => `
              <tr>
                <td>${escapeHtml(team.team)}</td>
                <td>${renderPositionOddsCell(team.positions[1])}</td>
                <td>${renderPositionOddsCell(team.positions[2])}</td>
                <td>${renderPositionOddsCell(team.positions[3])}</td>
                <td>${renderPositionOddsCell(team.positions[4])}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

function renderPositionOddsCell(probability) {
  const pct = Number(probability) || 0;
  return `<div class="position-odds-cell"><span class="position-odds-bar" style="width: ${pct.toFixed(1)}%;"></span><span class="position-odds-label">${pct.toFixed(1)}%</span></div>`;
}

// Wires the projected-tables <-> position-odds toggle button.
function wireGroupTablesToggle(container) {
  const section = container.querySelector("[data-group-tables-section]");
  if (!section) return;
  const button = section.querySelector("#group-tables-toggle");
  const heading = section.querySelector("#group-tables-heading");
  if (!button || !heading) return;
  const tablesView = section.querySelector('[data-group-tables-view="tables"]');
  const oddsView = section.querySelector('[data-group-tables-view="odds"]');
  button.addEventListener("click", () => {
    const showOdds = button.getAttribute("data-view") === "tables";
    if (showOdds) {
      button.setAttribute("data-view", "odds");
      button.textContent = "Show Projected Tables";
      heading.textContent = "Group Position Odds";
      if (tablesView) tablesView.style.display = "none";
      if (oddsView) oddsView.style.display = "";
    } else {
      button.setAttribute("data-view", "tables");
      button.textContent = "Show Position Odds";
      heading.textContent = "Projected Group Tables";
      if (oddsView) oddsView.style.display = "none";
      if (tablesView) tablesView.style.display = "";
    }
  });
}

// Single group table: marks the top two and qualifying third-place teams.
function renderGroupTable(group) {
  const teams = group.teams || [];
  return `
    <div class="world-cup-table-card">
      <h5>Group ${escapeHtml(group.group)}</h5>
      <div class="table-scroll">
        <table class="standings-table world-cup-table">
          <thead>
            <tr>
              <th>Team</th><th>P</th><th>W</th><th>D</th><th>L</th><th>GF</th><th>GA</th><th>GD</th><th>Pts</th>
            </tr>
          </thead>
          <tbody>
            ${teams.map((team) => `
              <tr class="${team.qualified ? "qualified-row" : ""}">
                <td>${escapeHtml(team.team)}</td>
                <td>${numberCell(team.P)}</td>
                <td>${numberCell(team.W)}</td>
                <td>${numberCell(team.D)}</td>
                <td>${numberCell(team.L)}</td>
                <td>${numberCell(team.GF)}</td>
                <td>${numberCell(team.GA)}</td>
                <td>${numberCell(team.GD)}</td>
                <td><strong>${numberCell(team.Pts)}</strong></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

// Third-place table: shows which third-place teams advance into the Round of 32.
function renderBestThirdTable(bestThirdRows) {
  if (!bestThirdRows.length) return "";
  return `
    <div class="world-cup-section">
      <h4>Best Third-Place Teams</h4>
      <div class="table-scroll">
        <table class="standings-table world-cup-table third-place-table">
          <thead>
            <tr>
              <th>Rank</th><th>Group</th><th>Team</th><th>P</th><th>W</th><th>D</th><th>L</th><th>GF</th><th>GA</th><th>GD</th><th>Pts</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            ${bestThirdRows.map((team) => `
              <tr class="${team.qualified ? "qualified-row" : ""}">
                <td>${numberCell(team.third_rank || team.rank)}</td>
                <td>${escapeHtml(team.group || "")}</td>
                <td>${escapeHtml(team.team)}</td>
                <td>${numberCell(team.P)}</td>
                <td>${numberCell(team.W)}</td>
                <td>${numberCell(team.D)}</td>
                <td>${numberCell(team.L)}</td>
                <td>${numberCell(team.GF)}</td>
                <td>${numberCell(team.GA)}</td>
                <td>${numberCell(team.GD)}</td>
                <td><strong>${numberCell(team.Pts)}</strong></td>
                <td>${team.qualified ? "Qualified" : "Eliminated"}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

// Group fixtures: a row of buttons at the top, one per group, lets the user
// filter the upcoming fixtures down to a single group. Defaults to the first
// group that has upcoming games.
function renderGroupFixtures(fixtures) {
  if (!fixtures.length) return "";
  const now = Date.now();
  const isUpcoming = (match) => {
    const dateValue = match.match_date || match.match_datetime_utc;
    if (!dateValue) return true;
    const ts = new Date(dateValue).getTime();
    return Number.isFinite(ts) ? ts >= now : true;
  };
  const upcoming = fixtures.filter(isUpcoming);
  const byGroup = {};
  for (const match of upcoming) {
    const group = match.group || match.group_letter || "";
    if (!byGroup[group]) byGroup[group] = [];
    byGroup[group].push(match);
  }
  const groupKeys = Object.keys(byGroup).sort((a, b) => a.localeCompare(b));
  if (!groupKeys.length) {
    return `
      <div class="world-cup-section">
        <h4>Group Fixtures</h4>
        <p class="info-message">No upcoming group fixtures available yet.</p>
      </div>
    `;
  }
  const groupsPayload = encodeURIComponent(JSON.stringify(byGroup));
  const buttons = groupKeys.map((key, idx) => `
    <button type="button" class="group-fixtures-btn ${idx === 0 ? "active" : ""}" data-group-fixtures-key="${escapeHtml(key)}" aria-pressed="${idx === 0 ? "true" : "false"}">
      ${escapeHtml(key)}
    </button>
  `).join("");
  return `
    <div class="world-cup-section" data-group-fixtures-section>
      <div class="world-cup-section-header">
        <h4>Group Fixtures</h4>
      </div>
      <div class="group-fixtures-buttons" role="tablist" aria-label="Filter group fixtures" data-group-fixtures-payload="${groupsPayload}">
        ${buttons}
      </div>
      <div class="world-cup-fixture-grid" data-group-fixtures-view></div>
    </div>
  `;
}

// Wires the group-fixtures buttons to render only the selected group's
// upcoming fixtures.
function wireGroupFixturesFilter(container) {
  const section = container.querySelector("[data-group-fixtures-section]");
  if (!section) return;
  const buttonsRow = section.querySelector(".group-fixtures-buttons");
  const view = section.querySelector("[data-group-fixtures-view]");
  if (!buttonsRow || !view) return;
  let byGroup = {};
  try {
    byGroup = JSON.parse(decodeURIComponent(buttonsRow.getAttribute("data-group-fixtures-payload") || "{}"));
  } catch (err) {
    byGroup = {};
  }
  const buttons = buttonsRow.querySelectorAll(".group-fixtures-btn");
  const renderGroup = (key) => {
    const matches = byGroup[key] || [];
    if (!matches.length) {
      view.innerHTML = '<p class="info-message">No upcoming fixtures for this group.</p>';
      return;
    }
    view.innerHTML = matches.map(renderFixtureCard).join("");
  };
  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      buttons.forEach((b) => {
        b.classList.remove("active");
        b.setAttribute("aria-pressed", "false");
      });
      button.classList.add("active");
      button.setAttribute("aria-pressed", "true");
      renderGroup(button.getAttribute("data-group-fixtures-key"));
    });
  });
  // Default to the first group's upcoming fixtures.
  const first = buttons[0];
  if (first) renderGroup(first.getAttribute("data-group-fixtures-key"));
}

// Fixture card: shows score, winner, venue, and model probabilities.
function renderFixtureCard(match) {
  const home = match.display_home_team || match.home_team;
  const away = match.display_away_team || match.away_team;
  const winner = match.winner_team || resultWinnerLabel(match, home, away);
  return `
    <article class="world-cup-match-card">
      <div class="match-card-top">
        <span>${escapeHtml(formatDate(match.match_date || match.match_datetime_utc))}</span>
        <span>${escapeHtml(match.venue || "Venue TBD")}</span>
      </div>
      <div class="match-card-score">
        <span>${escapeHtml(home)}</span>
        <strong>${numberCell(match.pred_home_goals)}-${numberCell(match.pred_away_goals)}</strong>
        <span>${escapeHtml(away)}</span>
      </div>
      <div class="match-card-meta">Winner: ${escapeHtml(winner || "Draw")}</div>
      ${renderProbabilityBar(match)}
      ${renderProbabilityLabels(match)}
    </article>
  `;
}

// Knockout section: renders each round in bracket order and highlights the final winner.
// All rounds except the Final are collapsed by default; click the header to expand.
function renderKnockoutRounds(knockout) {
  const stages = [
    ["knockout_round_playoffs", "Knockout Play-offs"],
    ["round_of_32", "Round of 32"],
    ["round_of_16", "Round of 16"],
    ["quarterfinals", "Quarterfinals"],
    ["semifinals", "Semifinals"],
    ["third_place", "Third Place"],
    ["final", "Final"],
  ];
  const visibleStages = stages
    .filter(([key]) => Array.isArray(knockout[key]) && knockout[key].length);
  if (!visibleStages.length) return "";

  const html = visibleStages
    .map(([key, label], idx) => {
      // Final is always shown; everything else is collapsed by default.
      const isFinal = key === "final";
      return renderKnockoutRound(label, knockout[key], { open: isFinal, stageKey: key, idx });
    })
    .join("");

  return `
    <div class="world-cup-section">
      <h4>Knockout Rounds</h4>
      ${html}
    </div>
  `;
}

// Knockout round: wrapped in a <details> element so the user can collapse/expand
// each round. The Final is open by default; all other rounds are collapsed.
function renderKnockoutRound(label, matches, options) {
  const { open = false, stageKey = "", idx = 0 } = options || {};
  const openAttr = open ? " open" : "";
  return `
    <details class="world-cup-round" data-stage="${escapeHtml(stageKey)}"${openAttr}>
      <summary><span class="world-cup-round-label">${escapeHtml(label)}</span><span class="world-cup-round-hint">${open ? "" : "(click to expand)"}</span></summary>
      <div class="world-cup-fixture-grid">
        ${matches.map(renderKnockoutCard).join("")}
      </div>
    </details>
  `;
}

// Knockout card: uses the winner field from the generated bracket when available.
function renderKnockoutCard(match) {
  const home = match.home_team;
  const away = match.away_team;
  const winner = match.winner || resultWinnerLabel(match, home, away);
  return `
    <article class="world-cup-match-card knockout-card">
      <div class="match-card-top">
        <span>${escapeHtml(match.label || match.stage || "Knockout Match")}</span>
        <span>${escapeHtml(match.stage || "")}</span>
      </div>
      <div class="match-card-score">
        <span class="${winner === home ? "winner-team" : ""}">${escapeHtml(home)}</span>
        <strong>${numberCell(match.pred_home_goals)}-${numberCell(match.pred_away_goals)}</strong>
        <span class="${winner === away ? "winner-team" : ""}">${escapeHtml(away)}</span>
      </div>
      <div class="match-card-meta">Advances: ${escapeHtml(winner || "TBD")}</div>
      ${renderProbabilityBar(match)}
      ${renderProbabilityLabels(match)}
    </article>
  `;
}

// Probability bar: normalizes decimal or percentage probability values.
function renderProbabilityBar(match) {
  const home = normalizeProbability(match.prob_home);
  const draw = normalizeProbability(match.prob_draw);
  const away = normalizeProbability(match.prob_away);
  return `
    <div class="world-cup-probabilities" aria-label="Prediction probabilities">
      <span style="width: ${home}%;" title="Home ${home.toFixed(1)}%"></span>
      <span style="width: ${draw}%;" title="Draw ${draw.toFixed(1)}%"></span>
      <span style="width: ${away}%;" title="Away ${away.toFixed(1)}%"></span>
    </div>
  `;
}

// Probability labels: explicit H/D/A percentages under the bar.
function renderProbabilityLabels(match) {
  const home = normalizeProbability(match.prob_home);
  const draw = normalizeProbability(match.prob_draw);
  const away = normalizeProbability(match.prob_away);
  return `
    <div class="match-card-probability-labels" aria-label="Prediction percentages">
      <span><strong>H</strong> ${home.toFixed(1)}%</span>
      <span><strong>D</strong> ${draw.toFixed(1)}%</span>
      <span><strong>A</strong> ${away.toFixed(1)}%</span>
    </div>
  `;
}

// Helpers: keep formatting safe and consistent across generated HTML.
function getFinalWinner(finalMatches) {
  return Array.isArray(finalMatches) && finalMatches[0] ? finalMatches[0].winner : "";
}

function resultWinnerLabel(match, home, away) {
  if (match.predicted_result === "H") return home;
  if (match.predicted_result === "A") return away;
  return "Draw";
}

function normalizeProbability(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return 0;
  return number <= 1 ? number * 100 : number;
}

function formatPercentValue(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(1)}%` : "0.0%";
}

function formatDate(value) {
  if (!value) return "Date TBD";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function formatDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
}

function numberCell(value) {
  return value === null || value === undefined || value === "" ? "-" : escapeHtml(value);
}

function escapeHtml(text) {
  const map = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  };
  return String(text).replace(/[&<>"']/g, (character) => map[character]);
}

document.addEventListener("DOMContentLoaded", () => {
  // Route templates load with active classes, then shared.js keeps navigation state aligned.
  if (typeof activateTab === "function") activateTab("world-cup");
  loadWorldCupProjection();
});
