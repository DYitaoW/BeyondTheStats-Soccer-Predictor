// World Cup page behavior: loads the saved projection and renders each tournament section.
async function loadWorldCupProjection() {
  const viewEl = document.getElementById("world-cup-view");
  if (!viewEl) return;

  viewEl.innerHTML = '<p class="loading-message">Loading World Cup projection...</p>';

  try {
    // Fetch the projection JSON exposed by Flask from Data/Predictions.
    const response = await fetch("/api/world-cup");
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      viewEl.innerHTML = `<p class="error-message">${escapeHtml(data.error || "Failed to load World Cup projection data.")}</p>`;
      return;
    }
    displayWorldCupData(data, viewEl);
  } catch (error) {
    console.error("Error loading World Cup data:", error);
    viewEl.innerHTML = `<p class="error-message">Error loading World Cup projection: ${escapeHtml(error.message)}</p>`;
  }
}

// Main renderer: keeps the page useful even when optional projection fields are absent.
function displayWorldCupData(data, container) {
  const knockout = data.knockout || {};
  const champion = data.champion || getFinalWinner(knockout.final);
  let html = "";

  html += renderProjectionSummary(data, champion);
  html += renderWinnerOdds(data.simulations?.winner_probabilities || {});
  html += renderRules(data.rules_summary || []);
  html += renderGroupTables(data.group_tables || []);
  html += renderBestThirdTable(data.third_place_table || data.best_third_place || data.best_third_teams || []);
  html += renderGroupFixtures(data.group_fixtures || []);
  html += renderKnockoutRounds(knockout);

  container.innerHTML = html || '<p class="info-message">No World Cup projection data is available yet.</p>';
}

// Summary card: surfaces the fields users care about before the long tables.
function renderProjectionSummary(data, champion) {
  const generated = formatDateTime(data.generated_at_utc);
  const simulations = data.simulations?.simulations_run;
  return `
    <div class="world-cup-summary">
      <div class="world-cup-summary-item">
        <span class="summary-label">Projected Champion</span>
        <strong>${escapeHtml(champion || "TBD")}</strong>
      </div>
      <div class="world-cup-summary-item">
        <span class="summary-label">Competition</span>
        <strong>${escapeHtml(data.competition || "FIFA/World Cup")}</strong>
      </div>
      <div class="world-cup-summary-item">
        <span class="summary-label">Generated</span>
        <strong>${escapeHtml(generated || "Unknown")}</strong>
      </div>
      <div class="world-cup-summary-item">
        <span class="summary-label">Simulations</span>
        <strong>${escapeHtml(simulations ? String(simulations) : "Not run")}</strong>
      </div>
    </div>
  `;
}

// Winner odds: lists the highest simulated title chances when simulation output exists.
function renderWinnerOdds(winnerProbabilities) {
  const entries = Object.entries(winnerProbabilities)
    .sort((left, right) => Number(right[1]) - Number(left[1]))
    .slice(0, 12);
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

// Rules summary: keeps the generator assumptions visible on the page.
function renderRules(rules) {
  if (!rules.length) return "";
  return `
    <div class="world-cup-section">
      <h4>Tournament Rules</h4>
      <ul class="world-cup-rules">
        ${rules.map((rule) => `<li>${escapeHtml(rule)}</li>`).join("")}
      </ul>
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
        <table class="standings-table world-cup-table">
          <thead>
            <tr>
              <th>Rank</th><th>Group</th><th>Team</th><th>P</th><th>GD</th><th>GF</th><th>Pts</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            ${bestThirdRows.map((team) => `
              <tr class="${team.qualified ? "qualified-row" : ""}">
                <td>${numberCell(team.third_rank || team.rank)}</td>
                <td>${escapeHtml(team.group || "")}</td>
                <td>${escapeHtml(team.team)}</td>
                <td>${numberCell(team.P)}</td>
                <td>${numberCell(team.GD)}</td>
                <td>${numberCell(team.GF)}</td>
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

// Group fixtures: gives users the actual predicted match list behind the standings.
function renderGroupFixtures(fixtures) {
  if (!fixtures.length) return "";
  return `
    <div class="world-cup-section">
      <h4>Group Fixtures</h4>
      <div class="world-cup-fixture-grid">
        ${fixtures.map(renderFixtureCard).join("")}
      </div>
    </div>
  `;
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
    </article>
  `;
}

// Knockout section: renders each round in bracket order and highlights the final winner.
function renderKnockoutRounds(knockout) {
  const stages = [
    ["round_of_32", "Round of 32"],
    ["round_of_16", "Round of 16"],
    ["quarterfinals", "Quarterfinals"],
    ["semifinals", "Semifinals"],
    ["third_place", "Third Place"],
    ["final", "Final"],
  ];
  const html = stages
    .filter(([key]) => Array.isArray(knockout[key]) && knockout[key].length)
    .map(([key, label]) => renderKnockoutRound(label, knockout[key]))
    .join("");
  if (!html) return "";

  return `
    <div class="world-cup-section">
      <h4>Knockout Rounds</h4>
      ${html}
    </div>
  `;
}

// Knockout round table: avoids draw labels because knockout projections resolve tied model output.
function renderKnockoutRound(label, matches) {
  return `
    <div class="world-cup-round">
      <h5>${escapeHtml(label)}</h5>
      <div class="world-cup-fixture-grid">
        ${matches.map(renderKnockoutCard).join("")}
      </div>
    </div>
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
