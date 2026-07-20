// Cups page: reuses the World Cup renderer for every supported cup competition.
const CUP_PROJECTION_CONFIGS = [
  { key: "ucl", label: "Champions League", competition: "UEFA/Champions League", aliases: ["UEFA/Champions League", "Europe/Champions League"] },
  { key: "uel", label: "Europa League", competition: "UEFA/Europa League", aliases: ["UEFA/Europa League", "Europe/Europa League"] },
  { key: "uecl", label: "Conference League", competition: "UEFA/Conference League", aliases: ["UEFA/Conference League", "Europe/Conference League"] },
  { key: "fa-cup", label: "FA Cup", competition: "England/FA Cup", aliases: ["England/FA Cup"] },
  { key: "league-cup", label: "League Cup", competition: "England/League Cup", aliases: ["England/League Cup"] },
];

let activeCupCompetition = CUP_PROJECTION_CONFIGS[0].competition;
let cupTablesCache = null;

function cupConfigForCompetition(competition) {
  return CUP_PROJECTION_CONFIGS.find((config) => config.aliases.includes(competition)) || CUP_PROJECTION_CONFIGS[0];
}

function primaryCupCompetition(config, payload) {
  if (!config || !payload?.tables) return config.competition;
  return config.aliases.find((name) => payload.tables[name]) || config.competition;
}

function renderCupTabs() {
  const tabsEl = document.getElementById("cup-projection-tabs");
  if (!tabsEl) return;

  tabsEl.innerHTML = CUP_PROJECTION_CONFIGS.map((config) => {
    const competition = cupTablesCache ? primaryCupCompetition(config, cupTablesCache) : config.competition;
    const active = cupConfigForCompetition(activeCupCompetition).key === config.key;
    return `
      <button
        class="tab-btn${active ? " active" : ""}"
        type="button"
        data-cup-competition="${escapeHtml(competition)}"
      >${escapeHtml(config.label)}</button>
    `;
  }).join("");
}

async function ensureCupTablesCache() {
  if (cupTablesCache) return cupTablesCache;
  const response = await fetch("/api/league-tables?mode=cups");
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || "Failed to load projected cup tables.");
  }
  cupTablesCache = data;
  return cupTablesCache;
}

async function loadCupPage() {
  const viewEl = document.getElementById("cup-projection-view");
  if (!viewEl || typeof loadTournamentProjection !== "function") return;

  try {
    const payload = await ensureCupTablesCache();
    const config = cupConfigForCompetition(activeCupCompetition);
    const competition = primaryCupCompetition(config, payload);
    activeCupCompetition = competition;
    renderCupTabs();

    const projectedRows = payload.tables?.[competition] || [];
    await loadTournamentProjection({
      viewEl,
      competition,
      projectedRows,
      loadingMessage: `Loading ${config.label} projection...`,
      errorMessage: `Failed to load ${config.label} projection data.`,
    });
  } catch (error) {
    console.error("Error loading cup page:", error);
    viewEl.innerHTML = `<p class="error-message">${escapeHtml(error.message)}</p>`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  if ((document.body?.dataset?.activePage || "").trim() !== "cups") return;
  if (typeof activateTab === "function") activateTab("cups");

  const tabsEl = document.getElementById("cup-projection-tabs");
  if (tabsEl) {
    tabsEl.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-cup-competition]");
      if (!button) return;
      activeCupCompetition = button.getAttribute("data-cup-competition") || activeCupCompetition;
      await loadCupPage();
    });
  }

  loadCupPage();
});
