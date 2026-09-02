// League tables page: direct loads hydrate data and wire view toggles.
document.addEventListener("DOMContentLoaded", () => {
  if (typeof activateTab === "function") {
    activateTab("league-table");
  }
  if (typeof updateTableViewToggleLabel === "function") {
    updateTableViewToggleLabel();
  }
  if (typeof updateTablePositionOddsToggleLabel === "function") {
    updateTablePositionOddsToggleLabel();
  }
});
