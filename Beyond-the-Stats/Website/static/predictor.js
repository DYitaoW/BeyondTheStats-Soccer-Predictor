// Predictor page hook: reserved for predictor-only enhancements.
document.addEventListener('DOMContentLoaded', () => {
  // Ensure predictor tab is visibly active on direct page loads.
  if (typeof activateTab === 'function') activateTab('predictor');
});
