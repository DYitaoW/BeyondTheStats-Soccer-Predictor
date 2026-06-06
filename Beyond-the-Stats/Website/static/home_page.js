// Home page hook: reserved for home-specific behaviors.
document.addEventListener('DOMContentLoaded', () => {
  // Keep Home tab state explicit when this page loads directly.
  if (typeof activateTab === 'function') activateTab('home');
});
