// World Cup page hook: reserved for world-cup page behavior.
document.addEventListener('DOMContentLoaded', () => {
  // Keep world-cup tab visibly active on direct loads.
  if (typeof activateTab === 'function') activateTab('world-cup');
});
