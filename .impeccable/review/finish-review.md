# Studio finish review — 5 October 2026

## 1. Disposition

**disposition: fix**

The interface has a coherent review-bench structure and should receive one batch of the four material fixes below. A wholesale visual rebuild is not warranted by the supplied product brief or actual render. This is not a ship verdict, a whole-product approval, or a closed comp gate.

All required source captures are valid: desktop 1440×900, mobile 390×844, user 424×1714, and comp comparison 1536×1024. They show the intended studio with loaded media. The desktop/mobile first-view screenshots intentionally capture an internally scrolling application; the taller user capture exposes the composer. No blank, black, wrong-route, or entrance-animation capture defect requires recapture before this review.

## 2. Strengths and scores

Scores are reviewer judgments out of 10 for the supplied studio surface, not measurements or claims about other routes.

| Dimension | Score | Evidence |
| --- | ---: | --- |
| Product clarity | 8 | Start image, subject action, camera, duration, and one obvious generation action; optional motion video and end frame are explicitly disclosed. |
| Visual hierarchy | 8 | Dominant loaded video, narrow composer, quiet charcoal ground, copper action, consistent thin dividers. |
| Craft and consistency | 8 | Self-hosted Space Grotesk, controlled radii, themed focus/selection/scrollbars, restrained drawer reveal, no ornamental hero metrics. |
| Desktop task composition | 7 | Generation fits above the fold, but the take strip's selection controls are cut off at 1440×900. |
| Mobile task access | 6 | Readable full-width media and controls; composition requires scrolling past a complete monitor, job, and take strip without a direct edit action. |
| Keyboard continuity | 6 | Shared nested-dialog owner exists; the Tools drawer loses the opener on close. |
| Direction fidelity | 8 | Inspect/direct/iterate is visible in the actual interface. |
| Exact comp fidelity | Unpassed | Current report is 54% overall, 30% structure; formal plates/hero workflow is open. |

The main image reference opens with one click. Generated takes also open with one click, while changing the monitor selection is a distinct action. Image generation supports optional identity, outfit, setting, style, and pose instructions without making roles mandatory. The optional text/shot desk preserves an independent creation path. The real running-job state and two generated videos are appropriate product content.

## 3. Concrete material fixes

| ID | Priority | Finding and required outcome | Verification for the verdict pass |
| --- | --- | --- | --- |
| F1 | P1 | **Tools drawer must restore focus to its opener on every dismissal.** In App.tsx the effect captures `document.activeElement` after the search input's `autoFocus` has run, so Escape restores the drawer input immediately before it is unmounted. The supplied user test confirms focus ends on body. Capture the actual opener before opening and restore it after close; cover Escape, close button, scrim, and navigation where the opener remains connected. Keep the focus trap. | Open Tools with keyboard from desktop and mobile trigger, traverse controls, close with Escape and the close button, and show that focus returns to the trigger. Check the AssetPicker too: its autoFocus search plus effect-based shared-dialog capture has the same source-level risk, although that failure was not independently observed here. |
| F2 | P1 | **Give mobile users a direct path to the composer.** At 390×844 the first viewport ends in the take strip; at 424 the composer starts around y920 and the generation action around y1600. The Create tab changes route but does not reach the editing task on the current route. Keep the deliberate media-first composition, but add a clearly named mobile edit/create action in the first viewport that scrolls the internal content area to the composer and moves focus to its heading or first meaningful field. Do not clear the draft or bypass required input validation. | At 390 and 424 widths, activate the visible action once and reach the existing composer with keyboard focus and its saved reference/action intact. Verify new/empty and existing-take states. |
| F3 | P2 | **Make selected-take identity explicit.** The monitor title is a long truncated generation prompt. Both strip controls say “View in monitor · 5.1 s”; thumbnails are labeled merely “video”. This loses the comp's useful take-number/duration/selected-state ritual, particularly when two takes share the same instruction. Show a concise take identifier plus measured duration in the toolbar and strip, and an explicit selected state beyond the copper border. Preserve the actual full asset name as accessible or expandable information. Give the selection and Compare toggle appropriate pressed/selected semantics. | Select each real take and show corresponding monitor identity, duration, and selection state; enter/leave comparison; confirm one-click enlargement still works independently. Do not invent a third take. |
| F4 | P2 | **Fit the desktop review loop at 1440×900.** The monitor is fixed at up to 515px tall and starts at y203 after the extra full-width creation row. The job then pushes Project takes to y818, cutting off the images and “View in monitor” buttons at the bottom. Size the monitor against available viewport height and/or compact the creation toolbar so the monitor, active-job summary, and at least one complete take with its selection action are visible in this common first viewport. Keep media dominant and all existing type/tool access. At 1536×1024 preserve the generous monitor. | Fresh desktop capture shows complete take selection controls at 1440×900; 1536×1024 remains balanced with no clipping or horizontal overflow. Test both active-job and idle state. |

Apply these as a single batch, recapture the same four paths, and return to this reviewer to score F1–F4 resolved, partial, or unresolved. Do not run a second detector or substitute an additional independent polish hunt.

## 4. Direction and comp fidelity

The builder-selected comp A is a critique reference chosen under delegated design authority, not a comp the human directly visually approved. The real build preserves its principal relationships: compact side navigation, left composer, dominant right monitor, take strip below, warm copper action on neutral dark ground. The interface also adds meaningful supported access to voice and node tools. The actual logo must prevail over the comp's invented book mark, and the avatar and unsupported motion-intensity slider should remain omitted.

There are genuine structural differences: creation modes moved into a full-width row above the workspace; the monitor begins about 80px lower; the active-job block further displaces the filmstrip; take identity and explicit selected caption were lost. F3 and F4 address the material effect of those differences. References are available in the composer/image workflow rather than as fictional decorative photos under the monitor. Preserve those functional reference roles; do not populate a synthetic strip to improve a score.

The current full-size comp-diff report reads **54.47% overall / 30.45% structure**, below the documented gate. Both its full side-by-side and heatmap were opened. All 26 region PNGs were opened again after the parent regenerated the output directory. The source captures are valid, but the region-pair implementation applies a `bestShift` global translation before cropping (`comp-diff.mjs`, compare, lines 277–291). With different real dancer frames, the resulting region pairs show the mode row in the brand crop and empty background in the navigation crop even though those elements are clearly present in the original capture. Thus some “missing” chrome labels are registration artifacts, not proof of removed features. Whole-frame scores remain as captured and must still be reported as failed; this does not convert them into a pass.

Build state remains **plates open**, with take-3 at 38% against its mock region; hero, sections, motion, responsive, and review are pending. There is no hero diff. The detector result `[]` accompanies `COMP_ROUND_OPEN`; it establishes no formal comp closure. The open workflow is a recorded limitation that must survive the handoff and user-facing completion claim. Do not force or retroactively mark gates passed. Do not repair it by replacing the user's dynamic media with static plates or adding unsupported controls. Passing the four finish fixes can support a scoped interface verdict; it cannot by itself close the comp workflow.

## 5. Verification scope and limits

This was a fresh source-and-image review without browser access. Read PRODUCT.md, direction.md, craft-floor.md, new-work.md, build state/spec/final report, relevant App/Studio/Generate/ScriptDesk/Storyboard/ui/Lightbox/studio.css source, and the research evidence document. Opened every required capture, studio-1.png, final side-by-side, heatmap, and every region image.

The parent reports all 18 routes findable/searchable, one-click viewer exercised, persisted SRT→two-shot links and times/images, real identity-plus-outfit image→video, comparison playback, and no console errors. Final full-suite result reported: 733 passed, 3 skipped, receipt `D:/LocalAI/qa/prospero-redesign-20261005/final-tests.xml`. These support preservation/function but are not independent browser verification by this reviewer.

The research document distinguishes advertised hosted-service capabilities from measured local results and does not claim service-equivalent quality. Its two real videos and contact-sheet/QA measurements support the tested cases only. This review does not certify artistic video quality, all model/backend combinations, every route's visual finish, light mode, Spanish long-copy behavior, comparison timing over entire playback, screen-reader output, or a full accessibility audit. The supplied captures are dark/English populated Studio; those are the visual scope of this verdict.

## Fix-batch verdict attempt — capture validity

**disposition: recapture**

- Replace `.impeccable/review/desktop.png` (1440×900) and `.impeccable/review/comp-size.png` (1536×1024). Both updated files visibly contain a native video loading spinner over the selected dancer; the desktop video controls are also absent. Settle and pause the selected video at a decoded frame before capturing. Preserve the real active job, selected take, complete strip selection controls, and dimensions. No source change is requested by this capture defect.
- `.impeccable/review/mobile.png` and `.impeccable/review/user-424.png` were opened and appear settled; they need no replacement for this reason.

No F1–F4 scores bind on this invalid evidence. Resume the scoped fix-list verdict after opening the replacements. Formal comp gates remain open.

## Fix-batch verdict — round 1, settled replacements

### 1. Disposition

**disposition: fix** — scoped exclusively to the original F1–F4 list. Desktop and comp-size were replaced with settled, decoded, paused video frames and opened again. The mobile and user-424 images previously opened remain the current evidence. The earlier recapture instruction is satisfied.

### 2. Strengths and scores

| Fix | Score | Evidence |
| --- | --- | --- |
| F1 | Resolved | App captures each Tools trigger in its click handler and restores the connected opener during effect cleanup; scrim prevents default focus stealing. Shared useDialog captures the opener on initial render before autofocus. Parent manually verifies desktop Escape/close/scrim, mobile Escape, and nested AssetPicker→viewer→Escape→picker→Escape→Choose focus continuity. |
| F2 | Resolved | “Compose a take” is clearly present at y225 in both mobile captures. The action is unconditional in the take desk, including empty states, scrolls to the existing composer, and focuses the action textarea without changing its value. Parent verifies one activation at 390 and preservation of the complete saved action; the user-424 image shows the focused textarea. |
| F3 | Resolved | Monitor says “Take 02 · 5.1 s” with Selected; strip entries have take numbers and actual durations, and selected caption/action. Selection and Compare use aria-pressed. Full original name remains in title and the existing viewer. Parent verifies switching Take02 and toggling comparison; enlargement remains independently available. |
| F4 | Partial | All three complete thumbnail/selection controls now fit in 1440×900 with the real active job. At 1536×1024 the larger monitor remains balanced. However, the 350px desktop monitor clips the actual decoded video above and below: the head/raised hand and feet are cut off, and playback controls disappear. |

### 3. Concrete remaining material fix

Complete **F4** by constraining the monitor's grid track and child intrinsic minimum dimensions. The video's box must fit the actual bounded monitor before object-fit:contain can preserve the frame. A minmax(0,1fr) row and min-height:0/min-width:0 on media and comparison wrappers are an appropriate implementation direction, subject to the resulting render. Keep the full subject, native playback controls, active-job summary, and complete take-selection controls simultaneously visible at 1440×900; keep the generous 1536×1024 view. This is a correction of the listed F4 regression, not a new polish finding.

### 4. Direction and comp fidelity

The new take labels recover the comp's useful review ritual. The shorter desktop monitor makes the full loop accessible, but clipping its media defeats the direction's inspection promise. Formal plates/hero gates remain open. This fix-list verdict neither reruns nor passes exact comp fidelity and does not expand scope to Spaces or any other surface.

### 5. Verification scope and limits

Opened all four supplied updated captures, then both replacement desktop files after video settling. Inspected only source relevant to F1–F4 and the reported manual outcomes. Parent reports TypeScript rebuild green and unchanged 733 passed/3 skipped. No second detector and no new raster assets are reported. This reviewer still has no browser. F1–F3 are resolved at this evidence scope; F4 remains partial. Recapture the same four paths after the targeted F4 correction and return for its scoped verdict.

## Fix-batch verdict — round 2, F4 only

### 1. Disposition

**disposition: ship** — bounded to the Studio F1–F4 fix list. F4 is now resolved; F1–F3 retain the resolved scores from round 1. This is not a new whole-surface audit or an exact-comp approval.

### 2. Strengths and scores

**F4: resolved.** The 1440×900 monitor contains the whole decoded video frame, including the raised hand/head and feet, and native playback controls. All four complete take thumbnails and selection actions remain visible in the first viewport. The 1536×1024 capture keeps a larger, balanced monitor with the full frame and controls. Mobile media also remain contained.

### 3. Concrete material fixes

No further correction is owed against F4. The source sets a minmax(0,1fr) monitor grid row, zero intrinsic minimum height/width on video, and zero minimum height on comparison wrappers while preserving object-fit:contain. This fixes the named clipping cause without expanding the monitor's bounded height. Stop Studio polish at this fix-list scope.

### 4. Direction and comp fidelity

The inspect/direct/iterate review loop now retains its complete media and visible take actions at the reviewed desktop height. The Spaces navigation label is outside this bounded verdict; its expanded node surface requires its separate review. Formal plates/hero and exact-comp gates remain open. The earlier 54% overall/30% structure report is historical comparison evidence, not a measurement of these updated captures, and no new fidelity pass is claimed.

### 5. Verification scope and limits

Opened the supplied final files: desktop.png (1440×900), mobile.png (390×844), user-viewport.png (the current 424-wide user capture, replacing user-424.png in this packet), and comp-size.png (1536×1024). All show loaded media with no loading spinner. Read only the source relevant to the F4 intrinsic sizing correction. Current captures show an idle take desk even though the global header reports two active jobs; the earlier round-1 active-job capture proved complete strip actions fit at the same bounded monitor height, and the final intrinsic sizing correction does not change that height or job geometry. This active-state conclusion combines earlier capture evidence with the narrow source change, not a new active-job capture.

No browser inspection, new detector, new video-quality assessment, or other-route audit was performed. Parent reports the source correction built successfully. The supported completion statement is: **the reviewer scored all four Studio finish fixes resolved**. It does not establish a passed comp workflow or comprehensive product approval.
