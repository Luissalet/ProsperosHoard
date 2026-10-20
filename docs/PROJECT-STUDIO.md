# Project dashboard and clip studio

Selecting a project always opens **Project**. Its dashboard shows project videos with a direct **Edit video** action, recent assets and cast. **+ Character** and **+ Group** open inline editors; existing entries edit in the same place. The full cast manager remains available for the other cast operations.

**Create clip** opens video creation directly. Choose or upload a start image, write its motion and generate in the video node. Reference nodes connect automatically; drag their headings to arrange the canvas, pan or use zoom/fit controls. Optional motion-video and end-frame nodes accept uploads and library choices. Use one optional guide per generation. Camera, duration, engine and seed remain available, as do take selection, comparison, enlargement and download.

The video node shows queued, waiting for GPU, running, failed, cancelled and completed states. Reloading recovers the last job before enabling another generation. A failed status lookup offers retry and does not imply that the generation stopped. A completed job loads its own output; a deliberately selected previous take remains selected when returning to the studio. The preview identifies that take while another job runs.

Generated clips are project assets. **Create clip with this shot** starts from the storyboard's frame; it does not automatically replace that production's clip. To render the production's own shots, use the production controls and its existing take-selection workflow.

Video production keeps **Storyboard**, **View** and **Text and takes** as primary sections. The process outline, quality and history remain reachable through their labelled disclosures. On mobile, clip references and video controls stack vertically rather than shrinking the graph.

Verification used an isolated synthetic demo, native stdio MCP and hidden browser captures at 390, 1440 and 1883 pixels, including light/dark themes. The Python suite passed 1114 tests (4 skipped); frontend build and 20 Node tests passed. This verifies interaction and job transport, not image/video model quality. `scripts/screenshots.py` was replaced by equivalent browser-tool captures because direct Playwright browser control is unavailable in this environment.
