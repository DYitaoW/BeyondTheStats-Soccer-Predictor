// World Cup page hook: reserved for world-cup page behavior.

async function loadWorldCupProjection() {
  const viewEl = document.getElementById('world-cup-view');
  if (!viewEl) return;

  try {
    const response = await fetch('/api/world-cup');
    if (!response.ok) {
      viewEl.innerHTML = '<p class="error">Failed to load World Cup projection data.</p>';
      return;
    }
    const data = await response.json();
    displayWorldCupData(data, viewEl);
  } catch (error) {
    console.error('Error loading World Cup data:', error);
    viewEl.innerHTML = '<p class="error">Error loading World Cup projection: ' + error.message + '</p>';
  }
}

function displayWorldCupData(data, container) {
  let html = '';

  // Display rules summary
  if (data.rules_summary && data.rules_summary.length > 0) {
    html += '<div class="world-cup-section"><h4>Tournament Rules</h4><ul>';
    data.rules_summary.forEach(rule => {
      html += `<li>${escapeHtml(rule)}</li>`;
    });
    html += '</ul></div>';
  }

  // Display group tables
  if (data.group_tables && data.group_tables.length > 0) {
    html += '<div class="world-cup-section"><h4>Group Tables</h4>';
    data.group_tables.forEach(group => {
      html += renderGroupTable(group);
    });
    html += '</div>';
  }

  // Display knockout rounds
  if (data.knockout) {
    html += '<div class="world-cup-section"><h4>Knockout Rounds</h4>';
    ['round_of_32', 'round_of_16', 'quarterfinals', 'semifinals', 'third_place', 'final'].forEach(stage => {
      if (data.knockout[stage] && data.knockout[stage].length > 0) {
        html += renderKnockoutRound(stage, data.knockout[stage]);
      }
    });
    html += '</div>';
  }

  container.innerHTML = html;
}

function renderGroupTable(group) {
  let html = `<div class="group-table" style="margin-bottom: 20px;">
    <h5>Group ${group.group}</h5>
    <table class="standings-table" style="width: 100%; border-collapse: collapse;">
      <thead>
        <tr>
          <th style="text-align: left; padding: 8px; border-bottom: 1px solid var(--card-border);">Team</th>
          <th style="text-align: center; padding: 8px; border-bottom: 1px solid var(--card-border);">P</th>
          <th style="text-align: center; padding: 8px; border-bottom: 1px solid var(--card-border);">W</th>
          <th style="text-align: center; padding: 8px; border-bottom: 1px solid var(--card-border);">D</th>
          <th style="text-align: center; padding: 8px; border-bottom: 1px solid var(--card-border);">L</th>
          <th style="text-align: center; padding: 8px; border-bottom: 1px solid var(--card-border);">GF</th>
          <th style="text-align: center; padding: 8px; border-bottom: 1px solid var(--card-border);">GA</th>
          <th style="text-align: center; padding: 8px; border-bottom: 1px solid var(--card-border);">GD</th>
          <th style="text-align: center; padding: 8px; border-bottom: 1px solid var(--card-border);">Pts</th>
        </tr>
      </thead>
      <tbody>`;

  group.teams.forEach(team => {
    html += `<tr style="border-bottom: 1px solid var(--card-border);">
      <td style="text-align: left; padding: 8px;">${escapeHtml(team.team)}</td>
      <td style="text-align: center; padding: 8px;">${team.P}</td>
      <td style="text-align: center; padding: 8px;">${team.W}</td>
      <td style="text-align: center; padding: 8px;">${team.D}</td>
      <td style="text-align: center; padding: 8px;">${team.L}</td>
      <td style="text-align: center; padding: 8px;">${team.GF}</td>
      <td style="text-align: center; padding: 8px;">${team.GA}</td>
      <td style="text-align: center; padding: 8px;">${team.GD}</td>
      <td style="text-align: center; padding: 8px; font-weight: bold;">${team.Pts}</td>
    </tr>`;
  });

  html += '</tbody></table></div>';
  return html;
}

function renderKnockoutRound(stage, matches) {
  const stageNames = {
    'round_of_32': 'Round of 32',
    'round_of_16': 'Round of 16',
    'quarterfinals': 'Quarterfinals',
    'semifinals': 'Semifinals',
    'third_place': 'Third Place',
    'final': 'Final'
  };

  let html = `<div class="knockout-round" style="margin-bottom: 20px;">
    <h5>${escapeHtml(stageNames[stage] || stage)}</h5>
    <div style="overflow-x: auto;">
      <table class="knockout-table" style="width: 100%; border-collapse: collapse;">
        <thead>
          <tr>
            <th style="text-align: left; padding: 8px; border-bottom: 1px solid var(--card-border);">Match</th>
            <th style="text-align: left; padding: 8px; border-bottom: 1px solid var(--card-border);">Home Team</th>
            <th style="text-align: center; padding: 8px; border-bottom: 1px solid var(--card-border);">Result</th>
            <th style="text-align: left; padding: 8px; border-bottom: 1px solid var(--card-border);">Away Team</th>
            <th style="text-align: center; padding: 8px; border-bottom: 1px solid var(--card-border);">Winner</th>
            <th style="text-align: center; padding: 8px; border-bottom: 1px solid var(--card-border);">Probability</th>
          </tr>
        </thead>
        <tbody>`;

  matches.forEach(match => {
    const resultStr = match.pred_home_goals + '-' + match.pred_away_goals;
    const winnerText = match.winner || '—';
    const homeProb = (match.prob_home * 100).toFixed(1);
    const awayProb = (match.prob_away * 100).toFixed(1);
    const probStr = match.predicted_result === 'H' ? homeProb + '%' : awayProb + '%';

    // Ensure no draws in knockout rounds
    let resultDisplay = '';
    if (match.predicted_result === 'H') {
      resultDisplay = '✓ Home';
    } else if (match.predicted_result === 'A') {
      resultDisplay = '✓ Away';
    } else {
      resultDisplay = 'Draw (ERROR)'; // This shouldn't happen in knockout
    }

    html += `<tr style="border-bottom: 1px solid var(--card-border);">
      <td style="text-align: left; padding: 8px;">${escapeHtml(match.label)}</td>
      <td style="text-align: left; padding: 8px;">${escapeHtml(match.home_team)}</td>
      <td style="text-align: center; padding: 8px; font-weight: bold;">${resultStr}</td>
      <td style="text-align: left; padding: 8px;">${escapeHtml(match.away_team)}</td>
      <td style="text-align: center; padding: 8px; color: #4CAF50;">${escapeHtml(winnerText)}</td>
      <td style="text-align: center; padding: 8px;">${probStr}</td>
    </tr>`;
  });

  html += '</tbody></table></div></div>';
  return html;
}

function escapeHtml(text) {
  const map = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;'
  };
  return String(text).replace(/[&<>"']/g, m => map[m]);
}

document.addEventListener('DOMContentLoaded', () => {
  // Keep world-cup tab visibly active on direct loads.
  if (typeof activateTab === 'function') activateTab('world-cup');
  
  // Load World Cup projection data
  loadWorldCupProjection();
});
