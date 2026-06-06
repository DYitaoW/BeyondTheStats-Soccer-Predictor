// Position odds page hook: reserved for position-odds behavior.
document.addEventListener('DOMContentLoaded', () => {
  // Keep position-odds tab visibly active on direct loads.
  if (typeof activateTab === 'function') activateTab('position-odds');
});
