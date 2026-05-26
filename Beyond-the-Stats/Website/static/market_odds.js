// Market page hook: reserved for market-page behavior.
document.addEventListener('DOMContentLoaded', () => {
  // Keep market tab visibly active on direct loads.
  if (typeof activateTab === 'function') activateTab('market');
});
