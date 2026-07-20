// About page hook: reserved for about-page behavior.
document.addEventListener('DOMContentLoaded', () => {
  // Keep about tab visibly active on direct loads.
  if (typeof activateTab === 'function') activateTab('about');
});
