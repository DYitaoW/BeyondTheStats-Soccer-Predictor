// League tables page hook: reserved for league-table page behavior.
document.addEventListener('DOMContentLoaded', () => {
  // Keep league-table tab visibly active on direct loads.
  if (typeof activateTab === 'function') activateTab('league-table');
});
