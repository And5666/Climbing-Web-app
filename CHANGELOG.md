# Changelog

## Unreleased

- Tries cost less: multipliers are 1.00 / 0.99 / 0.98 / 0.96 with a
  0.94 floor at 5+, so grade always beats tries — a V8 in 5+ (564)
  outscores a flash V7 (550). V4 flash stays 400, V4 on 5+ is 376
  now. Existing stored points are untouched; `recalc_points`
  re-scores on the new curve.
- Admin editor back on its original pan/zoom + tap handling after the
  shared-module version stopped registering marker taps.
- Hold colours in rainbow order (bold top row, duller below, black /
  brown / neutrals last, Custom picker after) as large grid boxes.
- Edit panel reachable on phones: capped with a vh fallback, swipes
  on it scroll the panel (never the map), opens scrolled into view.
- Main map zooms out a little past the fit-to-wall scale.
- Main map taps only open when the release lands inside the visible
  dot (overlaps go to the nearest centre); taps outside every marker
  open nothing.
- Board Hide/Remove buttons sit in a labelled Actions cell inside
  the table.
- Six new review-only cheat flags: send-rate outliers vs the gym
  distribution, grade-weighted pace floors, flash-rate drift with
  burst pairing, grade-jump velocity, batch-logged session
  clustering, and peer-relative sends-per-visit z-scores.
- Tries cap at 5+: the stepper tops out at 5, the server rejects
  higher, and everything 5 and over displays as 5+. Scoring already
  floored there, so old higher rows keep their points.
- Admin edit panel is a bottom sheet like the map's: slides up into
  view on open, grabber drags it down to dismiss, form scrolls
  inside without ever moving the map.
- Main map colliders are exactly the marker dots (fat halo removed);
  overlaps open the topmost marker, taps outside open nothing, and
  no tap box or focus ring flashes in Chromium.
- Admin pan bounds match the main map's overscroll give.

## 1.0.0

First versioned release.

- Points: tries matter only a little now — `grade_base × tries
  multiplier` (1.00 / 0.96 / 0.93 / 0.91 / 0.90 floor at 5+), with
  three grade modes (`flat`, `gentle` default, `linear`) and the tries
  table in one settings block. V4 flash = 400, V4 on 5+ tries = 360.
  Official grade only; mystery/unrated stay flat. New `recalc_points`
  command recomputes every ascent from stored grade + tries.
- Map zoom/pan rebuilt as one shared module (`wall-view.js`) for the
  main and admin pages: single transform on the photo+overlay
  container, zoom around pointer/pinch centre, clamped pan, constant
  on-screen tap targets, double-tap zoom.
- Tapping a climb opens that climb (admin mechanism); the close-climb
  chooser is removed. Hovered/pressed markers come to the front.
- Admin markers clamp inside the photo while dragging and on save
  (server too); `clamp_markers` fixes existing strays. Coordinates
  unchanged.
- Hold colours: every shade visible at a glance in family-grouped
  swatch grids (47 presets) + Custom hex entry. Boards/stats/logs
  show names, never raw hex. Grade text picks black/white for
  contrast.
- Logging a send updates the sheet and markers in place (no reload);
  marker taps open the tapped climb (pointer capture waits for a
  real drag; taps hit-test the visible dots, never paint order).
  Both pages stay usable if the zoom module fails to load. Admin map
  orders first on phones.
- Admin markers use the map's tap mechanism (release within 12px /
  600ms opens; drags save). Fixed a hang when a stored colour sat
  outside the palette, and disabled double-tap zoom on the editor.
- Wall nudges 96px per side zoomed out. Odd-hours flags follow gym
  hours 10:00–22:00. Review tables scroll instead of running
  off-screen.
- Anti-cheat flags (staff only, never auto-punishing, never visible
  to users): ascent audit ledger, 8 rules in one config dict,
  suspicion badges + review actions on the board admin page, void /
  hide / restore with logged notes, per-user climb log page with CSV,
  `recompute_flags` command. Tries validate 1–99 with rate limiting.
- Profile: Log out moved here (POST + CSRF); footer shows the version
  and a feedback contact.
