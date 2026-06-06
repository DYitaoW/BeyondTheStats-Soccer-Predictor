// Cups page hook: reserved for cup-page behavior.
document.addEventListener('DOMContentLoaded', () => {
  // Keep cups tab visibly active on direct loads.
  if (typeof activateTab === 'function') activateTab('cups');
});
