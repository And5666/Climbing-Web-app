/* shared pan and zoom for the map and the admin wall so they dont act
   diffrent. one transform with transform-origin 0 0 on the wrapper
   keeps the photo and the svg overlay together. wheel and pinch zoom,
   double tap zooms in, pan gets clamped after every move. taps still
   reach the markers. */
(function () {
    'use strict';

    var MAX_SCALE = 8;
    // how far you can push past the edge when zoomed right out
    var OVERSCROLL = 96;

    function clampScale(s, min) {
        return Math.min(MAX_SCALE, Math.max(min, s));
    }

    function createWallView(opts) {
        var viewport = opts.viewport;
        var content = opts.content;
        var onTransform = opts.onTransform || null;
        // the admin editor turns this off so quick taps dont zoom
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
        // no transitions here, they fight the pointer and feel jumpy
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
            // fit the wall in the box but let it go a bit smaller
            return 0.8 * Math.min(1, s.vw / s.cw, s.vh / s.ch);
        }

        function clampPan() {
            // run this after every move. if the wall is smaller than the
            // box it sits in the middle, if its bigger keep the edges inside
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
            // window or image changed size, keep the zoom and redraw
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
            // keep the bit under the cursor under the cursor
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

        function reset() {
            // fit wall button
            var s = sizes();
            state.scale = minScale();
            state.x = (s.vw - s.cw * state.scale) / 2;
            state.y = (s.vh - s.ch * state.scale) / 2;
            schedule();
        }

        function setView(scale, x, y) {
            // restoring a saved view so clamp the old numbers to this box
            state.scale = clampScale(
                Number(scale) || minScale(), minScale());
            state.x = Number(x) || 0;
            state.y = Number(y) || 0;
            schedule();
        }

        function forgetPointer(e) {
            pointers.delete(e.pointerId);
            delete captured[e.pointerId];
            if (pointers.size < 2) pinch = null;
        }

        viewport.addEventListener('pointerdown', function (e) {
            if (!panEnabled) return;
            // dont capture yet or markers never recieve there tap
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
                // only capture once its a real drag
                try {
                    viewport.setPointerCapture(e.pointerId);
                    captured[e.pointerId] = true;
                } catch (err) { /* older browsers: track anyway */ }
            }
            if (pointers.size === 2) {
                // pinch zoom around the pinch centre
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

        // stop the page scrolling and slow the wheel down a bit
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
            reset: reset,
            getState: function () {
                return {scale: state.scale, x: state.x, y: state.y};
            },
            setView: setView,
            onTransform: function (fn) { onTransform = fn; },
            // Marker drags (admin) pause panning so the marker moves
            // instead of the map; taps still reach the marker.
            pause: function () { panEnabled = false; },
            resume: function () { panEnabled = true; },
        };
    }

    /* markers stay about the same size on screen when you zoom. the
       label and hit circle shrink with the ring so taps still work */
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
        // other css colour names, let the browser work it out
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

    /* pick black or white text depending on the tape colour. extraHexes
       turns names like orange into there hex */
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

    /* move the marker you tapped to the top */
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
