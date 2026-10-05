## verdict

F1 — resolved. The same `spaces-mobile.png` (390×844) and `spaces-user.png` (424×1714) were reopened and are valid. The selected clip now fills the available width with readable prompt text, port labels and generation/settings actions instead of shrinking to fit its entire height. The short viewport starts at the node heading and permits vertical panning; `spaces-mobile-preview.png` visibly demonstrates reaching the large preview at the retained editing scale. `Spaces.tsx:936` shares the width-based mobile focus behavior between Focus on and node double-click, while desktop retains its prior fit behavior. Controls and media remain present. The reported build and repository screenshot checks passed. No detector was rerun and no UI was edited by this reviewer.

Regressions introduced by the F1 batch: none evident in the supplied recaptures. The existing user-positioned branch overlap remains the previously accepted free-canvas adaptation.

## remaining

Clear for the scored F1 fix. This ship verdict covers the scored Spaces mobile-focus fix, not a new whole-surface review or an approval of Studio reproduction. The separate Studio comp gates remain open. The Operate QUALITY BAR card is still unavailable; the prior direction-based ceiling limitation remains and no card-specific ceiling pass is claimed.

disposition: ship
