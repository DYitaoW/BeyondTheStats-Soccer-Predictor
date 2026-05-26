// preload home widgets so upcoming cards and summary stats render immediately
preloadHomeData();

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

// render winners directly from API data so home page is independent of shared tab logic
function renderHomeWinners(data) {
    if (!winnerView) return;
    const tables = data?.tables || {};
    const leagues = Object.keys(tables).filter((name) => name !== "__mls_bracket__").sort((a, b) => a.localeCompare(b));
    if (!leagues.length) {
        winnerView.innerHTML = "<p>No winner data available.</p>";
        return;
    }
    let html = '<table class="league-table"><thead><tr><th>League</th><th>Predicted Winner</th><th>Win Chance</th></tr></thead><tbody>';
    for (const league of leagues) {
        const winner = pickLeagueWinner(tables[league]);
        html += `<tr><td>${league}</td><td>${winner ? winner.team : "N/A"}</td><td>${winner ? asPct(winner.win_league_pct) : "0%"}</td></tr>`;
    }
    html += "</tbody></table>";
    winnerView.innerHTML = html;
}

// fetch and render winners for selected dataset
async function loadHomeWinners() {
    if (!winnerDataset || !winnerView) return;
    winnerView.textContent = "Loading league winners...";
    try {
        const response = await fetch(`/api/league-tables?mode=${encodeURIComponent(winnerDataset.value)}`);
        const data = await response.json();
        if (!response.ok || !data?.ok) {
            winnerView.textContent = "Failed to load league winners.";
            return;
        }
        renderHomeWinners(data);
    } catch (_error) {
        winnerView.textContent = "Failed to load league winners.";
    }
}

// bind home winner selector only on pages that include the control
if (winnerDataset) {
    winnerDataset.addEventListener("change", loadHomeWinners);
    loadHomeWinners();
}
