// Head-to-head page hook: reserved for H2H page behavior.
document.addEventListener('DOMContentLoaded', () => {
  // Keep H2H tab visibly active on direct loads.
  if (typeof activateTab === 'function') activateTab('h2h');
});
