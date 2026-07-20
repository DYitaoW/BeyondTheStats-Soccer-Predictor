// Upcoming page hook: reserved for upcoming-match page behavior.
document.addEventListener('DOMContentLoaded', () => {
  // Keep upcoming tab visibly active on direct loads.
  if (typeof activateTab === 'function') activateTab('global');
});
