"use strict";

window.addEventListener("DOMContentLoaded", () => {
    new QWebChannel(qt.webChannelTransport, channel => {
        const backend = channel.objects.backend;
        const bounds = L.latLngBounds([[-85.05112878, -180], [85.05112878, 180]]);
        const map = L.map("map", {
            attributionControl: false,
            maxBounds: bounds,
            maxBoundsViscosity: 1,
            minZoom: 1,
            maxZoom: 20,
            zoomControl: true
        });
        map.fitBounds(bounds, {animate: false});
        L.control.scale({imperial: true, metric: false}).addTo(map);
        map.createPane("basemap").style.zIndex = 150;
        L.geoJSON(window.AUTOORTHO_WORLD, {
            pane: "basemap",
            style: {
                color: "#637367", fillColor: "#344b3d",
                fillOpacity: 1, opacity: 0.7, weight: 0.6
            },
            onEachFeature(feature, layer) {
                const label = document.createElement("span");
                label.textContent = feature.properties.name;
                layer.bindTooltip(label, {sticky: true});
            }
        }).addTo(map);

        let nextRequest = 0;
        const pending = new Map();
        const CacheLayer = L.GridLayer.extend({
            createTile(coords, done) {
                const canvas = document.createElement("canvas");
                canvas.width = canvas.height = 256;
                canvas.mapCoords = coords;
                this.request(canvas, done);
                return canvas;
            },
            request(canvas, done) {
                if (canvas.mapRequest !== undefined) return;
                const id = ++nextRequest;
                const coords = canvas.mapCoords;
                canvas.mapRequest = id;
                pending.set(id, {canvas, done});
                backend.requestTile(id, coords.z, coords.x, coords.y);
            },
            refreshMissing() {
                Object.values(this._tiles).forEach(tile => {
                    if (tile.current && (
                        tile.el.dataset.cached !== "true" ||
                        Number(tile.el.dataset.sourceZoom) < tile.coords.z
                    )) {
                        this.request(tile.el, () => reportCoverage());
                    }
                });
            }
        });
        const imagery = new CacheLayer({
            tileSize: 256, noWrap: true, bounds, keepBuffer: 0,
            updateWhenIdle: true, updateWhenZooming: false
        });
        function reportCoverage() {
            const tiles = Object.values(imagery._tiles || {}).filter(t => t.current);
            const cached = tiles.filter(t => t.el.dataset.cached === "true");
            const lower = cached.filter(t => Number(t.el.dataset.sourceZoom) < t.coords.z);
            backend.reportCoverage(cached.length, tiles.length, lower.length);
        }
        imagery.on("tileunload", event => {
            const id = event.tile.mapRequest;
            if (id !== undefined) {
                pending.delete(id);
                backend.cancelTile(id);
                delete event.tile.mapRequest;
            }
            reportCoverage();
        });
        imagery.on("load", reportCoverage);
        backend.tile_ready.connect((id, result) => {
            const request = pending.get(id);
            if (!request) return;
            pending.delete(id);
            const {canvas, done} = request;
            if (!result.image) {
                delete canvas.mapRequest;
                done(null, canvas);
                reportCoverage();
                return;
            }
            const image = new Image();
            image.onload = () => {
                if (canvas.mapRequest !== id) return;
                delete canvas.mapRequest;
                const coords = canvas.mapCoords;
                const factor = 2 ** (coords.z - result.zoom);
                const width = image.naturalWidth / factor;
                const height = image.naturalHeight / factor;
                const sx = (coords.x % factor) * width;
                const sy = (coords.y % factor) * height;
                canvas.getContext("2d").drawImage(
                    image, sx, sy, width, height, 0, 0, 256, 256
                );
                canvas.dataset.cached = "true";
                canvas.dataset.sourceZoom = String(result.zoom);
                done(null, canvas);
                reportCoverage();
            };
            image.onerror = () => {
                delete canvas.mapRequest;
                backend.reportError("A cached JPEG could not be decoded.");
                done(null, canvas);
            };
            image.src = `data:image/jpeg;base64,${result.image}`;
        });
        backend.cache_changed.connect(() => imagery.redraw());
        backend.refresh_requested.connect(() => {
            map.invalidateSize();
            imagery.refreshMissing();
        });
        imagery.addTo(map);

        const position = document.getElementById("position");
        const trail = L.polyline([], {
            color: "#6da4e3", weight: 2, opacity: 0.8, interactive: false
        }).addTo(map);
        let marker = null;
        let lastPosition = null;
        let firstFix = true;
        let following = true;
        let connected = false;
        let lastUpdate = 0;
        let points = [];
        const plane = '<svg viewBox="0 0 36 36" aria-hidden="true">' +
            '<path d="M18 2 L21 14 L33 22 L33 25 L21 21 L21 29 L25 32 ' +
            'L25 34 L18 32 L11 34 L11 32 L15 29 L15 21 L3 25 L3 22 ' +
            'L15 14 Z"/></svg>';
        const unknownHeading = '<svg viewBox="0 0 36 36" aria-hidden="true">' +
            '<circle cx="18" cy="18" r="8"/><path d="M18 3V10M18 26V33' +
            'M3 18H10M26 18H33" fill="none"/></svg>';

        function pauseFollow() {
            following = false;
            backend.pauseFollow();
        }
        function centre() {
            if (lastPosition) map.panTo(lastPosition, {animate: false});
        }
        function disconnect() {
            connected = false;
            position.className = lastPosition ? "stale" : "";
            position.textContent = lastPosition
                ? "Telemetry lost - last known position"
                : "Waiting for X-Plane telemetry";
            if (marker) marker.getElement().classList.add("aircraft-stale");
            if (points.length && points[points.length - 1] !== null) points.push(null);
        }
        function updateFlight(state) {
            lastUpdate = performance.now();
            if (!state.connected) {
                disconnect();
                return;
            }
            const next = [state.lat, state.lon];
            const previous = points[points.length - 1];
            if (previous && (
                Math.abs(previous[1] - next[1]) > 180 ||
                map.distance(previous, next) > 50000
            )) {
                points = [];
            }
            if (!previous || previous[0] !== next[0] || previous[1] !== next[1]) {
                points.push(next);
                points = points.slice(-300);
                const segments = [[]];
                points.forEach(point => {
                    if (point === null) segments.push([]);
                    else segments[segments.length - 1].push(point);
                });
                trail.setLatLngs(segments);
            }
            connected = true;
            lastPosition = next;
            const knownHeading = state.heading !== null && state.heading !== undefined;
            const symbol = knownHeading ? plane : unknownHeading;
            const rotation = knownHeading ? state.heading : 0;
            const icon = L.divIcon({
                className: "aircraft-icon", iconSize: [36, 36], iconAnchor: [18, 18],
                html: `<span class="aircraft-symbol" style="transform:rotate(${rotation}deg)">${symbol}</span>`
            });
            if (!marker) marker = L.marker(next, {icon, keyboard: true}).addTo(map);
            else marker.setLatLng(next).setIcon(icon);
            const headingText = knownHeading ? ` | ${Math.round(state.heading)} degrees true` : "";
            marker.getElement().setAttribute("aria-label", `Aircraft at ${state.lat}, ${state.lon}${headingText}`);
            position.className = "live";
            position.textContent = `${state.lat.toFixed(5)}, ${state.lon.toFixed(5)}`;
            if (following) {
                if (firstFix) map.setView(next, 16, {animate: false});
                else centre();
            }
            firstFix = false;
        }
        map.on("dragstart", pauseFollow);
        map.getContainer().addEventListener("keydown", event => {
            if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
                pauseFollow();
            }
        });
        backend.follow_changed.connect(value => {
            following = value;
            if (following) centre();
        });
        backend.recenter_requested.connect(centre);
        backend.flight_changed.connect(updateFlight);
        setInterval(() => {
            if (connected && performance.now() - lastUpdate > 5000) disconnect();
        }, 1000);
        window.autoorthoMap = {map, imagery, trail, get marker() { return marker; }};
        backend.ready();
    });
});
