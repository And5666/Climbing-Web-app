/* Shared wall pan/zoom + marker helpers, used by BOTH the main map
   and the admin wall so the two pages can never drift apart again.
   No external dependency: one controller owns a single transform
   (translate + scale, transform-origin 0 0) on the one container
   holding the photo and the SVG overlay, so they can never desync.

   Root causes of the old main-page glitch, fixed here by
   construction:
   - the old code transformed the outer stack while hit-testing in
     screen space, and fired re-entrant moveTo nudges mid-gesture, so
     zooming into a corner jumped the image off-screen;
   - pan bounds were the library's, computed against stale box sizes.

   Behaviour:
   - wheel zooms around the pointer, pinch zooms around the pinch
     centre; double-tap zooms in (toggles back out when deep);
   - min scale fits the content to the container, max is 8x;
   - pan is clamped after EVERY update: content smaller than the
     viewport on an axis is centred, otherwise edges stay inside;
   - pointer events throughout, touch-action none on the container,
     non-passive wheel listener, updates batched with
     requestAnimationFrame, no CSS transitions while gesturing;
   - taps are never captured: pointer capture waits for a real drag,
     so the marker under the finger still gets its pointerup;
   - bounds recomputed on resize, orientation change, window load
     and any image load inside the viewport. */
(function () {
    'use strict';

    var MAX_SCALE = 8;
    // Even fully zoomed out the wall can be pushed around: this
    // many px of give beyond the edge on every side.
    var OVERSCROLL = 96;

    function clampScale(s, min) {
        return Math.min(MAX_SCALE, Math.max(min, s));
    }

    function createWallView(opts) {
        var viewport = opts.viewport;
        var content = opts.content;
        var onTransform = opts.onTransform || null;
        // Precision surfaces (the admin editor) opt out of double-tap
        // zoom: hurried taps must never reframe the wall mid-edit.
        var allowDoubleTap = opts.doubleTap !== false;

        var state = {scale: 1, x: 0, y: 0};
        var panEnabled = true;
        var raf = 0;
        var captured = {};
        var pointers = new Map();
        var pinch = null;
        var moved = 0;
        var downAt = 0;
        var lastTapAt = 0;
        var lastTapX = 0;
        var lastTapY = 0;

        viewport.style.touchAction = 'none';
        content.style.transformOrigin = '0 0';
        // No CSS transitions on the scene, ever: they run during
        // gestures and fight the pointer, which reads as a jump.
        content.style.transition = 'none';

        function sizes() {
            return {
                vw: viewport.clientWidth || 1,
                vh: viewport.clientHeight || 1,
                cw: content.offsetWidth || 1,
                ch: content.offsetHeight || 1,
            };
        }

        function minScale() {
            var s = sizes();
            // Fit-to-container, plus a little extra room to zoom
            // out: the whole wall with some space around it (the
            // content box already fills the viewport at scale 1, so
            // the fit is 1 except in odd aspect boxes).
            return 0.8 * Math.min(1, s.vw / s.cw, s.vh / s.ch);
        }

        function clampPan() {
            // Clamp after EVERY update. Smaller than the viewport on
            // an axis: rest centred, with a little overscroll give so
            // the wall never feels frozen when zoomed out. Larger:
            // keep the edges inside, with the same give.
            var s = sizes();
            var w = s.cw * state.scale;
            var h = s.ch * state.scale;
            if (w <= s.vw) {
                var restX = (s.vw - w) / 2;
                state.x = Math.min(restX + OVERSCROLL,
                    Math.max(restX - OVERSCROLL, state.x));
            } else {
                state.x = Math.min(OVERSCROLL,
                    Math.max(s.vw - w - OVERSCROLL, state.x));
            }
            if (h <= s.vh) {
                var restY = (s.vh - h) / 2;
                state.y = Math.min(restY + OVERSCROLL,
                    Math.max(restY - OVERSCROLL, state.y));
            } else {
                state.y = Math.min(OVERSCROLL,
                    Math.max(s.vh - h - OVERSCROLL, state.y));
            }
        }

        function apply() {
            raf = 0;
            clampPan();
            content.style.transform =
                'translate(' + state.x + 'px,' + state.y + 'px)' +
                ' scale(' + state.scale + ')';
            if (onTransform) onTransform(state.scale);
        }

        function schedule() {
            if (!raf) raf = requestAnimationFrame(apply);
        }

        function refresh() {
            // Recompute bounds (resize / orientation / image load):
            // keep the scale, re-clamp the pan, repaint once.
            state.scale = clampScale(state.scale, minScale());
            schedule();
        }

        function zoomAt(clientX, clientY, factor) {
            var rect = viewport.getBoundingClientRect();
            var px = clientX - rect.left;
            var py = clientY - rect.top;
            var old = state.scale;
            var next = clampScale(old * factor, minScale());
            if (next === old) return;
            // Zoom around the pointer: the content point under the
            // cursor stays under the cursor.
            var cx = (px - state.x) / old;
            var cy = (py - state.y) / old;
            state.scale = next;
            state.x = px - cx * next;
            state.y = py - cy * next;
            schedule();
        }

        function centerOn(contentX, contentY, scale) {
            var s = sizes();
            state.scale = clampScale(
                scale === undefined ? state.scale : scale, minScale());
            state.x = s.vw / 2 - contentX * state.scale;
            state.y = s.vh / 2 - contentY * state.scale;
            schedule();
        }

        function forgetPointer(e) {
            pointers.delete(e.pointerId);
            delete captured[e.pointerId];
            if (pointers.size < 2) pinch = null;
        }

        viewport.addEventListener('pointerdown', function (e) {
            if (!panEnabled) return;
            // No capture yet: grabbing the pointer here would
            // retarget the pointerup to the viewport and markers
            // would never receive their tap. Capture only once the
            // pointer has clearly started a drag (see pointermove).
            pointers.set(e.pointerId, {x: e.clientX, y: e.clientY});
            if (pointers.size === 1) {
                moved = 0;
                downAt = Date.now();
            } else if (pointers.size === 2) {
                var pts = Array.from(pointers.values());
                pinch = {
                    dist: Math.hypot(
                        pts[0].x - pts[1].x, pts[0].y - pts[1].y),
                    cx: (pts[0].x + pts[1].x) / 2,
                    cy: (pts[0].y + pts[1].y) / 2,
                };
            }
        });

        viewport.addEventListener('pointermove', function (e) {
            if (!panEnabled || !pointers.has(e.pointerId)) return;
            var prev = pointers.get(e.pointerId);
            var dx = e.clientX - prev.x;
            var dy = e.clientY - prev.y;
            pointers.set(e.pointerId, {x: e.clientX, y: e.clientY});
            moved += Math.abs(dx) + Math.abs(dy);
            if (!captured[e.pointerId] && moved > 12) {
                try {
                    viewport.setPointerCapture(e.pointerId);
                    captured[e.pointerId] = true;
                } catch (err) { /* older browsers: track anyway */ }
            }
            if (pointers.size === 2) {
                // Pinch: zoom around the pinch centre, pan with it.
                var pts = Array.from(pointers.values());
                var dist = Math.hypot(
                    pts[0].x - pts[1].x, pts[0].y - pts[1].y);
                var cx = (pts[0].x + pts[1].x) / 2;
                var cy = (pts[0].y + pts[1].y) / 2;
                if (pinch && pinch.dist > 0 && dist > 0) {
                    zoomAt(cx, cy, dist / pinch.dist);
                    state.x += cx - pinch.cx;
                    state.y += cy - pinch.cy;
                    schedule();
                }
                pinch = {dist: dist, cx: cx, cy: cy};
            } else if (pointers.size === 1) {
                state.x += dx;
                state.y += dy;
                schedule();
            }
        });

        function endPointer(e) {
            var wasSingle = pointers.size === 1 && pointers.has(e.pointerId);
            var held = Date.now() - downAt;
            forgetPointer(e);
            if (!wasSingle || pointers.size !== 0) return;
            // Double-tap zoom: two quick clean taps. Deep already:
            // toggle back out to fit instead of zooming forever.
            if (allowDoubleTap && moved < 24 && held < 600) {
                var gap = Date.now() - lastTapAt;
                var near = Math.hypot(
                    e.clientX - lastTapX, e.clientY - lastTapY) < 40;
                if (gap < 350 && near) {
                    lastTapAt = 0;
                    if (state.scale > 3) centerOn(
                        (e.clientX -
                            viewport.getBoundingClientRect().left -
                            state.x) / state.scale,
                        (e.clientY -
                            viewport.getBoundingClientRect().top -
                            state.y) / state.scale,
                        minScale());
                    else zoomAt(e.clientX, e.clientY, 2);
                } else {
                    lastTapAt = Date.now();
                    lastTapX = e.clientX;
                    lastTapY = e.clientY;
                }
            }
        }

        viewport.addEventListener('pointerup', endPointer);
        viewport.addEventListener('pointercancel', forgetPointer);

        // Non-passive so the page never scrolls/zooms under us.
        // Calm factor: full-speed wheel deltas jump at deep zoom.
        viewport.addEventListener('wheel', function (e) {
            e.preventDefault();
            var delta = Math.max(-80, Math.min(80, e.deltaY || 0));
            zoomAt(e.clientX, e.clientY, Math.exp(-delta * 0.0022));
        }, {passive: false});

        window.addEventListener('resize', refresh);
        window.addEventListener('orientationchange', refresh);
        window.addEventListener('load', refresh);
        viewport.querySelectorAll('img').forEach(function (img) {
            img.addEventListener('load', refresh);
        });

        refresh();
        return {
            getScale: function () { return state.scale; },
            getMinScale: minScale,
            refresh: refresh,
            centerOn: centerOn,
            zoomAt: zoomAt,
            onTransform: function (fn) { onTransform = fn; },
            // Marker drags (admin) pause panning so the marker moves
            // instead of the map; taps still reach the marker.
            pause: function () { panEnabled = false; },
            resume: function () { panEnabled = true; },
        };
    }

    /* Markers hold a steady on-screen size while zoomed (softer
       counter-scale): the grade label scales in lockstep with the
       ring and the invisible hit circle keeps a constant on-screen
       size so markers stay finger-tappable at any zoom. */
    var BASE_R = 7, BASE_FONT = 7, BASE_STROKE = 1.2, HIT_R = 20;

    function rescaleMarkers(root, scale) {
        var f = 1 / Math.pow(scale, 0.55);
        root.querySelectorAll('.climb-marker').forEach(function (m) {
            var c = m.querySelector('.climb-dot') || m.querySelector('circle');
            if (c) {
                c.setAttribute('r', (BASE_R * f).toFixed(2));
                c.setAttribute(
                    'stroke-width', (BASE_STROKE * f).toFixed(2));
            }
            var hit = m.querySelector('.climb-hit');
            if (hit) hit.setAttribute('r', (HIT_R * f).toFixed(2));
            var t = m.querySelector('text');
            if (t) t.setAttribute('font-size', (BASE_FONT * f).toFixed(2));
        });
    }

    function luminance(r, g, b) {
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    }

    function parseColour(value, extraHexes) {
        var raw = String(value || '').trim().toLowerCase();
        if (extraHexes && extraHexes[raw]) raw = extraHexes[raw];
        var m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(raw);
        if (m) {
            var hex = m[1];
            if (hex.length === 3) {
                hex = hex.split('').map(function (ch) {
                    return ch + ch;
                }).join('');
            }
            return [parseInt(hex.slice(0, 2), 16),
                    parseInt(hex.slice(2, 4), 16),
                    parseInt(hex.slice(4, 6), 16)];
        }
        // Any other CSS colour name: let the browser resolve it once
        // through a canvas-free computed style probe.
        try {
            var el = document.createElement('span');
            el.style.color = raw;
            el.style.display = 'none';
            document.body.appendChild(el);
            var parts = getComputedStyle(el).color.match(/[\d.]+/g);
            el.remove();
            if (parts && parts.length >= 3) {
                return [+parts[0], +parts[1], +parts[2]];
            }
        } catch (err) { /* fall through to white */ }
        return null;
    }

    /* Grade text inside the marker circle picks black or white for
       contrast against any tape colour. extraHexes maps preset names
       (e.g. {"orange": "#f08c00"}) so names resolve without a probe. */
    function applyMarkerContrast(root, extraHexes) {
        root.querySelectorAll('.climb-marker').forEach(function (m) {
            var dot = m.querySelector('.climb-dot') ||
                m.querySelector('circle');
            var label = m.querySelector('text');
            if (!dot || !label) return;
            var rgb = parseColour(
                dot.getAttribute('fill') || '', extraHexes);
            if (!rgb) return;
            label.setAttribute('fill', luminance(rgb[0], rgb[1], rgb[2]) > 0.6
                ? '#000000' : '#ffffff');
        });
    }

    /* Overlapping markers behave predictably: the hovered/pressed
       marker comes to the front (SVG paints document order). */
    function bringToFront(marker) {
        if (marker && marker.parentNode) {
            marker.parentNode.appendChild(marker);
        }
    }

    window.WallView = {
        create: createWallView,
        rescaleMarkers: rescaleMarkers,
        applyMarkerContrast: applyMarkerContrast,
        bringToFront: bringToFront,
    };
})();
