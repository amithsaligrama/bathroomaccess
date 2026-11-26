// Shared basemap setup. Bathroom markers and search remain in map.html.
// Load Leaflet, MapLibre GL JS and the Leaflet adapter before this file.
(function () {
    'use strict';

    window.addOpenFreeMapBasemap = function (map) {
        var status = document.getElementById('map-status');
        var basemap;

        function showStatus(message) {
            if (status) {
                status.textContent = message;
                status.hidden = false;
            }
        }

        if (typeof window.maplibregl === 'undefined' || typeof L.maplibreGL !== 'function') {
            showStatus('The background map could not load. You can still use the bathroom list below.');
            return null;
        }

        try {
            basemap = L.maplibreGL({
                style: 'https://tiles.openfreemap.org/styles/liberty',
                attributionControl: {
                    customAttribution:
                        '<a href="https://openfreemap.org/">OpenFreeMap</a> ' +
                        '&copy; <a href="https://www.openmaptiles.org/">OpenMapTiles</a> ' +
                        'Data from <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
                }
            });

            // Adapter 0.1.4 assumes construction succeeded when removing a layer.
            // Let Leaflet also clean up events if WebGL construction throws.
            var removeBasemap = basemap.onRemove;
            basemap.onRemove = function (leafletMap) {
                if (this.getMaplibreMap()) {
                    removeBasemap.call(this, leafletMap);
                } else {
                    var container = this.getContainer();
                    if (container && container.parentNode) {
                        container.parentNode.removeChild(container);
                    }
                }
            };

            basemap.addTo(map);
            basemap.getMaplibreMap().on('error', function (event) {
                console.warn('OpenFreeMap resource failed:', event.error || event);
                showStatus('Some background map details could not load. Refresh to try again, or use the bathroom list below.');
            });
            return basemap;
        } catch (error) {
            if (basemap && map.hasLayer(basemap)) {
                map.removeLayer(basemap);
            }
            console.warn('OpenFreeMap could not start:', error);
            showStatus('The background map is unavailable in this browser. Try another browser, or use the bathroom list below.');
            return null;
        }
    };
}());
