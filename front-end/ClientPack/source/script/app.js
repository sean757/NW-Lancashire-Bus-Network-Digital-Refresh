// Leaflet map and markers
const apiUrl = 'http://localhost:8080/api/v1';

/*
  Helper functions
  - Network helpers
  - UI helpers
  - Dropdown helpers
*/
async function getPossibleLocations(partialStr) {
    try {
        const response = await fetch(`${apiUrl}/stops/search?q=${encodeURIComponent(partialStr)}`);
        if (!response.ok) {
            throw new Error('Network response was not ok');
        }
        const locations = await response.json();
        return locations;
    } catch (error) {
        console.error('Error fetching locations:', error);
        return [];
    }
}

// Selected items and pending markers (if map not ready)
let selectedStartItem = null;
let selectedEndItem = null;
let pendingStartItem = null;
let pendingEndItem = null;

// Timers for debounced exact-match lookup
let fromInputTimer = null;
let toInputTimer = null;

/* Loading indicator helpers for inputs */
function ensureInputLoadingEl(input) {
    if (!input || !input.parentElement) return null;
    const parent = input.parentElement;
    let el = parent.querySelector('.input-loading');
    if (!el) {
        el = document.createElement('span');
        el.className = 'input-loading';
        el.setAttribute('aria-hidden', 'true');
        parent.appendChild(el);
    }
    return el;
}

function positionInputLoading(input, el) {
    if (!input || !el) return;
    // Calculate vertical center of the input relative to its parent (.input-group)
    const parentRect = input.parentElement.getBoundingClientRect();
    const inputRect = input.getBoundingClientRect();
    const top = input.offsetTop + (input.offsetHeight / 2);
    el.style.top = top + 'px';
}

function showInputLoading(input) {
    const el = ensureInputLoadingEl(input);
    if (!el) return;
    positionInputLoading(input, el);
    el.style.display = 'block';
}

function hideInputLoading(input) {
    if (!input || !input.parentElement) return;
    const el = input.parentElement.querySelector('.input-loading');
    if (el) el.style.display = 'none';
}

function extractLatLng(item) {
    if (!item) return null;
    // common property names
    const lat = item.lat || item.latitude || item.lat_deg || (item.geometry && item.geometry.coordinates && item.geometry.coordinates[1]);
    const lon = item.lon || item.longitude || item.lon_deg || (item.geometry && item.geometry.coordinates && item.geometry.coordinates[0]);
    if (lat == null || lon == null) return null;
    const la = parseFloat(lat);
    const lo = parseFloat(lon);
    if (Number.isNaN(la) || Number.isNaN(lo)) return null;
    return [la, lo];
}

/**
 * Create a lightweight location item from a Leaflet LatLng object (map click).
 * The returned object is compatible with extractLatLng / getLabelFromItem / addStartMarker / addEndMarker.
 */
function makeCustomItem(latlng) {
    const lat = latlng.lat.toFixed(5);
    const lon = latlng.lng.toFixed(5);
    return {
        lat: latlng.lat,
        lon: latlng.lng,
        stop_name: `${lat}, ${lon}`,
    };
}

function addStartMarker(item) {
    const coords = extractLatLng(item);
    selectedStartItem = item;
    if (!coords) return;
    if (!map) {
        pendingStartItem = item;
        return;
    }
    // remove existing marker
    if (startMarker) {
        map.removeLayer(startMarker);
        startMarker = null;
    }
    startMarker = L.circleMarker(coords, {
        radius: 8,
        fillColor: '#27AE60',
        color: '#ffffff',
        weight: 2,
        fillOpacity: 1
    }).addTo(map).bindPopup(getLabelFromItem(item));
    map.panTo(coords);
}

function addEndMarker(item) {
    const coords = extractLatLng(item);
    selectedEndItem = item;
    if (!coords) return;
    if (!map) {
        pendingEndItem = item;
        return;
    }
    if (endMarker) {
        map.removeLayer(endMarker);
        endMarker = null;
    }
    endMarker = L.circleMarker(coords, {
        radius: 8,
        fillColor: '#E74C3C',
        color: '#ffffff',
        weight: 2,
        fillOpacity: 1
    }).addTo(map).bindPopup(getLabelFromItem(item));
    map.panTo(coords);
}

// Accessible notification function (UI helper)
function showNotification(message) {
    const toast = document.getElementById('notificationToast');
    toast.textContent = message;
    toast.classList.add('show');

    setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}

/**
 * Update the map click hint control text to reflect what the next click will do.
 * Called after each map click and after adding/clearing markers.
 */
function updateMapClickHint() {
    const hint = document.getElementById('mapClickHint');
    if (!hint) return;
    const t = translations[currentLang] || translations.en;
    if (!selectedStartItem && !selectedEndItem) {
        hint.textContent = t.clickHintStart;
    } else if (selectedStartItem && !selectedEndItem) {
        hint.textContent = t.clickHintEnd;
    } else {
        hint.textContent = t.clickHintReset;
    }
}

// Map / state variables
let map = null;
let startMarker = null;
let endMarker = null;

// Bus stop markers layer
let busStopLayerGroup = null;
// Zoom level at which bus stop icons become visible
const BUS_STOP_ZOOM_THRESHOLD = 14;
// Debounce timer for updateBusStopMarkers
let busStopUpdateTimer = null;

// DOM selectors
let fromInput = document.getElementById('startPoint');
let toInput = document.getElementById('endPoint');

// Suggestions dropdown for `fromInput`
// Create container for suggestions appended to body so it can overlay the map
let fromSuggestions = document.getElementById('fromSuggestions');
if (!fromSuggestions) {
    fromSuggestions = document.createElement('div');
    fromSuggestions.id = 'fromSuggestions';
    fromSuggestions.className = 'suggestions-dropdown';
    // append to body to avoid stacking context issues with map panes
    document.body.appendChild(fromSuggestions);
}

// Suggestions dropdown for `toInput` (end point)
let toSuggestions = document.getElementById('toSuggestions');
if (!toSuggestions) {
    toSuggestions = document.createElement('div');
    toSuggestions.id = 'toSuggestions';
    toSuggestions.className = 'suggestions-dropdown';
    document.body.appendChild(toSuggestions);
}

function positionFromSuggestions() {
    const rect = fromInput.getBoundingClientRect();
    const scrollY = window.scrollY || window.pageYOffset;
    const scrollX = window.scrollX || window.pageXOffset;
    fromSuggestions.style.width = rect.width + 'px';
    fromSuggestions.style.left = (rect.left + scrollX) + 'px';
    fromSuggestions.style.top = (rect.bottom + scrollY + 6) + 'px';
}

function positionToSuggestions() {
    const rect = toInput.getBoundingClientRect();
    const scrollY = window.scrollY || window.pageYOffset;
    const scrollX = window.scrollX || window.pageXOffset;
    toSuggestions.style.width = rect.width + 'px';
    toSuggestions.style.left = (rect.left + scrollX) + 'px';
    toSuggestions.style.top = (rect.bottom + scrollY + 6) + 'px';
}

function clearFromSuggestions() {
    fromSuggestions.innerHTML = '';
    fromSuggestions.style.display = 'none';
    fromSuggestions.dataset.active = '-1';
}

function getLabelFromItem(item) {
    if (!item && item !== 0) return '';
    if (typeof item === 'string') return item;
    return item.name || item.label || item.display_name || item.stop_name || item.text || JSON.stringify(item);
}

function renderFromSuggestions(items) {
    clearFromSuggestions();
    if (!items || items.length === 0) return;
    const list = document.createElement('ul');
    list.setAttribute('role', 'listbox');
    list.className = 'suggestions-list';

    items.forEach((it, idx) => {
        const label = getLabelFromItem(it);
        const li = document.createElement('li');
        li.className = 'suggestion-item';
        li.setAttribute('role', 'option');
        li.setAttribute('data-index', String(idx));
        li.tabIndex = 0;
        li.textContent = label;
        li.addEventListener('click', () => {
            selectedStartItem = it;
            fromInput.value = label;
            clearFromSuggestions();
            fromInput.focus();
            // cancel any pending exact-match lookup and hide loader
            if (fromInputTimer) { clearTimeout(fromInputTimer); fromInputTimer = null; }
            hideInputLoading(fromInput);
            addStartMarker(it);
        });
        li.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                li.click();
            }
        });
        list.appendChild(li);
    });
    // store items for later reference (keyboard selection)
    fromSuggestions._items = items;

    fromSuggestions.appendChild(list);
    positionFromSuggestions();
    fromSuggestions.style.display = 'block';
}

function clearToSuggestions() {
    toSuggestions.innerHTML = '';
    toSuggestions.style.display = 'none';
}

function renderToSuggestions(items) {
    clearToSuggestions();
    if (!items || items.length === 0) return;
    const list = document.createElement('ul');
    list.setAttribute('role', 'listbox');
    list.className = 'suggestions-list';

    items.forEach((it, idx) => {
        const label = getLabelFromItem(it);
        const li = document.createElement('li');
        li.className = 'suggestion-item';
        li.setAttribute('role', 'option');
        li.setAttribute('data-index', String(idx));
        li.tabIndex = 0;
        li.textContent = label;
        li.addEventListener('click', () => {
            selectedEndItem = it;
            toInput.value = label;
            clearToSuggestions();
            toInput.focus();
            if (toInputTimer) { clearTimeout(toInputTimer); toInputTimer = null; }
            hideInputLoading(toInput);
            addEndMarker(it);
        });
        li.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                li.click();
            }
        });
        list.appendChild(li);
    });
    toSuggestions._items = items;

    toSuggestions.appendChild(list);
    positionToSuggestions();
    toSuggestions.style.display = 'block';
    toSuggestions.dataset.active = '-1';
}

/* Keyboard navigation helpers */
function setActiveSuggestion(container, idx) {
    const items = Array.from(container.querySelectorAll('.suggestion-item'));
    if (!items.length) return;
    if (idx < 0) idx = items.length - 1;
    if (idx >= items.length) idx = 0;
    // remove previous
    items.forEach((it) => {
        it.classList.remove('suggestion-active');
        it.setAttribute('aria-selected', 'false');
    });
    const chosen = items[idx];
    if (chosen) {
        chosen.classList.add('suggestion-active');
        chosen.setAttribute('aria-selected', 'true');
        chosen.focus();
        container.dataset.active = String(idx);
    }
}

function handleInputKeydown(e, inputEl, container, renderFnClear) {
    const items = Array.from(container.querySelectorAll('.suggestion-item'));
    if (!items.length) return;
    let active = parseInt(container.dataset.active || '-1', 10);
    if (e.key === 'ArrowDown') {
        e.preventDefault();
        active = isNaN(active) ? -1 : active;
        setActiveSuggestion(container, active + 1);
    } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        active = isNaN(active) ? 0 : active;
        setActiveSuggestion(container, active - 1);
    } else if (e.key === 'Enter') {
        // if an item is active, trigger click
        if (!isNaN(active) && active >= 0) {
            e.preventDefault();
            const chosen = items[active];
            if (chosen) chosen.click();
        }
    } else if (e.key === 'Escape') {
        // close
        container.style.display = 'none';
        container.dataset.active = '-1';
        inputEl.focus();
    }
}

// Attach keyboard handling to inputs
fromInput.addEventListener('keydown', (e) => handleInputKeydown(e, fromInput, fromSuggestions));
toInput.addEventListener('keydown', (e) => handleInputKeydown(e, toInput, toSuggestions));

/*
  Event listeners - dropdown
*/
fromInput.addEventListener('input', async () => {
    const q = fromInput.value.trim();
    // If user changed/cleared the input so it no longer exactly matches the selected item,
    // remove the marker and clear the selected state.
    if (selectedStartItem) {
        const selLabel = getLabelFromItem(selectedStartItem).trim().toLowerCase();
        if (q.toLowerCase() !== selLabel) {
            if (startMarker && map) {
                map.removeLayer(startMarker);
                startMarker = null;
            }
            selectedStartItem = null;
            pendingStartItem = null;
        }
    }
    if (q.length < 3) {
        clearFromSuggestions();
        return; // Wait for at least 3 characters
    }
    try {
        const locations = await getPossibleLocations(q);
        // Render first 5 suggestions
        renderFromSuggestions((locations || []).slice(0, 5));
    } catch (err) {
        console.error('Error fetching suggestions:', err);
        clearFromSuggestions();
    }
});

// Debounced exact-match lookup after 2s of no typing
fromInput.addEventListener('input', () => {
    if (fromInputTimer) clearTimeout(fromInputTimer);
    const q = fromInput.value.trim();
    // If there's nothing to search for, ensure loader is hidden
    if (!q) {
        hideInputLoading(fromInput);
        return;
    }
    // show loader for the 2s debounce period
    showInputLoading(fromInput);
    fromInputTimer = setTimeout(async () => {
        // timer fired -- hide loader
        fromInputTimer = null;
        hideInputLoading(fromInput);
        if (!q) return;
        // If already selected and matches, skip
        if (selectedStartItem && getLabelFromItem(selectedStartItem).trim().toLowerCase() === q.toLowerCase()) return;
        try {
            const locations = await getPossibleLocations(q);
            if (locations && locations.length) {
                const match = locations.find((it) => getLabelFromItem(it).trim().toLowerCase() === q.toLowerCase());
                if (match) {
                    selectedStartItem = match;
                    fromInput.value = getLabelFromItem(match);
                    clearFromSuggestions();
                    addStartMarker(match);
                }
            }
        } catch (err) {
            console.error('Exact-match lookup error (from):', err);
        }
    }, 2000);
});

// Hide suggestions when clicking outside
document.addEventListener('click', (e) => {
    if (!fromSuggestions.contains(e.target) && e.target !== fromInput) {
        clearFromSuggestions();
    }
    if (!toSuggestions.contains(e.target) && e.target !== toInput) {
        clearToSuggestions();
    }
});

// Reposition dropdown on scroll/resize and when input receives focus
window.addEventListener('resize', () => {
    if (fromSuggestions.style.display === 'block') positionFromSuggestions();
    if (toSuggestions.style.display === 'block') positionToSuggestions();
});
window.addEventListener('scroll', () => {
    if (fromSuggestions.style.display === 'block') positionFromSuggestions();
    if (toSuggestions.style.display === 'block') positionToSuggestions();
}, true);
fromInput.addEventListener('focus', () => {
    if (fromSuggestions.children.length) {
        positionFromSuggestions();
        fromSuggestions.style.display = 'block';
    }
});
toInput.addEventListener('focus', () => {
    if (toSuggestions.children.length) {
        positionToSuggestions();
        toSuggestions.style.display = 'block';
    }
});

// Input listener for `toInput`
toInput.addEventListener('input', async () => {
    const q = toInput.value.trim();
    // If user changed/cleared the input so it no longer exactly matches the selected item,
    // remove the marker and clear the selected state.
    if (selectedEndItem) {
        const selLabel = getLabelFromItem(selectedEndItem).trim().toLowerCase();
        if (q.toLowerCase() !== selLabel) {
            if (endMarker && map) {
                map.removeLayer(endMarker);
                endMarker = null;
            }
            selectedEndItem = null;
            pendingEndItem = null;
        }
    }
    if (q.length < 3) {
        clearToSuggestions();
        return; // Wait for at least 3 characters
    }
    try {
        const locations = await getPossibleLocations(q);
        // Render first 5 suggestions
        renderToSuggestions((locations || []).slice(0, 5));
    } catch (err) {
        console.error('Error fetching suggestions (to):', err);
        clearToSuggestions();
    }
});

// Debounced exact-match lookup after 2s of no typing
toInput.addEventListener('input', () => {
    if (toInputTimer) clearTimeout(toInputTimer);
    const q = toInput.value.trim();
    if (!q) {
        hideInputLoading(toInput);
        return;
    }
    showInputLoading(toInput);
    toInputTimer = setTimeout(async () => {
        toInputTimer = null;
        hideInputLoading(toInput);
        if (!q) return;
        if (selectedEndItem && getLabelFromItem(selectedEndItem).trim().toLowerCase() === q.toLowerCase()) return;
        try {
            const locations = await getPossibleLocations(q);
            if (locations && locations.length) {
                const match = locations.find((it) => getLabelFromItem(it).trim().toLowerCase() === q.toLowerCase());
                if (match) {
                    selectedEndItem = match;
                    toInput.value = getLabelFromItem(match);
                    clearToSuggestions();
                    addEndMarker(match);
                }
            }
        } catch (err) {
            console.error('Exact-match lookup error (to):', err);
        }
    }, 2000);
});


// Initialize map and click handlers
document.addEventListener('DOMContentLoaded', () => {
    loadAccessibilitySettings();
    initializeLeafletMap();
    initializePlannerToggle();
});

// Initialize map and click handlers
document.addEventListener('DOMContentLoaded', () => {
    loadAccessibilitySettings();
    loadLanguageSettings();
    initializeLeafletMap();
});


function initializeLeafletMap() {
    // Define North Lancashire boundaries
    // Covers Lancaster, Morecambe, Preston, Blackpool, Fylde, and surrounding areas
    const northLancashireBounds = [
        [53.5, -3.1],  // Southwest corner (Blackpool area)
        [54.3, -2.2]   // Northeast corner (Lancaster/Pennines)
    ];

    // Initialize the map centered on NW Lancashire (Preston area)
    // Preston coordinates: approximately 53.7632° N, 2.7031° W
    map = L.map('map', {
        maxBounds: northLancashireBounds,
        maxBoundsViscosity: 1.0,  // Makes bounds "hard" - prevents dragging outside
        zoomControl: false
    }).setView([53.7632, -2.7031], 10);

    // Add OpenStreetMap tiles
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 19,
        minZoom: 8
    }).addTo(map);

    // Fit the map to North Lancashire bounds on load
    map.fitBounds(northLancashireBounds);

    // If user selected items before map ready, add their markers now
    if (pendingStartItem) {
        addStartMarker(pendingStartItem);
        pendingStartItem = null;
    }
    if (pendingEndItem) {
        addEndMarker(pendingEndItem);
        pendingEndItem = null;
    }

    // Zoom hint control – tells the user to zoom in to see/select bus stops
    const zoomHint = L.control({ position: 'bottomleft' });
    zoomHint.onAdd = function () {
        const div = L.DomUtil.create('div', 'bus-stop-zoom-hint');
        div.setAttribute('aria-live', 'polite');
        div.textContent = '🔍 Zoom in to see and select bus stops on the map';
        return div;
    };
    zoomHint.addTo(map);

    function updateZoomHint() {
        const hint = document.querySelector('.bus-stop-zoom-hint');
        if (!hint) return;
        if (map.getZoom() >= BUS_STOP_ZOOM_THRESHOLD) {
            hint.style.display = 'none';
        } else {
            hint.style.display = 'block';
        }
    }

    // Load/refresh bus stop markers whenever the view changes (debounced)
    map.on('zoomend moveend', () => {
        updateZoomHint();
        if (busStopUpdateTimer) clearTimeout(busStopUpdateTimer);
        busStopUpdateTimer = setTimeout(() => {
            busStopUpdateTimer = null;
            updateBusStopMarkers();
        }, 300);
    });
    updateZoomHint();

    // Map click hint control – shows what the next map click will do
    const clickHintControl = L.control({ position: 'bottomright' });
    clickHintControl.onAdd = function () {
        const div = L.DomUtil.create('div', 'map-click-hint');
        div.setAttribute('aria-live', 'polite');
        div.id = 'mapClickHint';
        return div;
    };
    clickHintControl.addTo(map);
    updateMapClickHint();

    // Handle map clicks for start / end point selection.
    // 1st click  → set start point
    // 2nd click  → set end point
    // 3rd+ click → reset start (clear existing end) so the user can pick a new route
    map.on('click', (e) => {
        const item = makeCustomItem(e.latlng);
        const label = getLabelFromItem(item);
        const t = translations[currentLang] || translations.en;
        if (!selectedStartItem) {
            if (fromInputTimer) { clearTimeout(fromInputTimer); fromInputTimer = null; }
            fromInput.value = label;
            clearFromSuggestions();
            addStartMarker(item);
            showNotification(t.mapClickNotifyStart);
        } else if (!selectedEndItem) {
            if (toInputTimer) { clearTimeout(toInputTimer); toInputTimer = null; }
            toInput.value = label;
            clearToSuggestions();
            addEndMarker(item);
            showNotification(t.mapClickNotifyEnd);
        } else {
            // Both already set — update start and clear end so the user can pick a new route
            if (fromInputTimer) { clearTimeout(fromInputTimer); fromInputTimer = null; }
            selectedEndItem = null;
            toInput.value = '';
            if (endMarker) { map.removeLayer(endMarker); endMarker = null; }
            fromInput.value = label;
            clearFromSuggestions();
            addStartMarker(item);
            showNotification(t.mapClickNotifyReset);
        }
        updateMapClickHint();
    });
}

/**
 * Fetch bus stops within the current map bounds and render them as
 * clickable Leaflet markers.  Only active when zoom >= BUS_STOP_ZOOM_THRESHOLD.
 */
async function updateBusStopMarkers() {
    if (!map) return;

    // Below threshold – remove any existing stop markers and bail out
    if (map.getZoom() < BUS_STOP_ZOOM_THRESHOLD) {
        if (busStopLayerGroup) {
            busStopLayerGroup.clearLayers();
        }
        return;
    }

    const bounds = map.getBounds();
    const params = new URLSearchParams({
        min_lat: bounds.getSouth(),
        max_lat: bounds.getNorth(),
        min_lon: bounds.getWest(),
        max_lon: bounds.getEast(),
        limit: 200,
    });

    let stops = [];
    try {
        const res = await fetch(`${apiUrl}/stops/bounds?${params}`);
        if (res.ok) {
            stops = await res.json();
        }
    } catch (err) {
        console.error('Failed to fetch bus stops for map view:', err);
        return;
    }

    // Initialise the layer group once
    if (!busStopLayerGroup) {
        busStopLayerGroup = L.layerGroup().addTo(map);
    } else {
        busStopLayerGroup.clearLayers();
    }

    stops.forEach((stop) => {
        const lat = parseFloat(stop.latitude);
        const lon = parseFloat(stop.longitude);
        if (isNaN(lat) || isNaN(lon)) return;

        // Custom bus-stop icon (DivIcon so it works without external images)
        const icon = L.divIcon({
            className: 'bus-stop-icon',
            html: `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32" role="img" aria-label="Bus stop">
                <circle cx="16" cy="16" r="15" fill="#2E5090" stroke="white" stroke-width="2.5"/>
                <text x="16" y="21" font-family="Arial,sans-serif" font-size="15" font-weight="bold" fill="white" text-anchor="middle">B</text>
            </svg>`,
            iconSize: [32, 32],
            iconAnchor: [16, 16],
            popupAnchor: [0, -18],
        });

        const label = stop.stop_name + (stop.locality ? ` (${stop.locality})` : '');
        const marker = L.marker([lat, lon], {
            icon,
            title: label,
            alt: `Bus stop: ${label}`,
        });

        // Build popup with "Set as Start" / "Set as End" buttons
        const popupEl = document.createElement('div');
        popupEl.className = 'bus-stop-popup';

        const nameEl = document.createElement('strong');
        nameEl.textContent = label;
        popupEl.appendChild(nameEl);

        const btnGroup = document.createElement('div');
        btnGroup.className = 'bus-stop-popup-btns';

        const setStartBtn = document.createElement('button');
        setStartBtn.className = 'bus-stop-popup-btn bus-stop-popup-btn--start';
        setStartBtn.setAttribute('aria-label', `Set ${label} as start point`);
        setStartBtn.textContent = '📍 Set as Start';
        setStartBtn.addEventListener('click', () => {
            fromInput.value = label;
            addStartMarker(stop);
            marker.closePopup();
            showNotification(`Start point set to: ${label}`);
            updateMapClickHint();
        });

        const setEndBtn = document.createElement('button');
        setEndBtn.className = 'bus-stop-popup-btn bus-stop-popup-btn--end';
        setEndBtn.setAttribute('aria-label', `Set ${label} as end point`);
        setEndBtn.textContent = '🏁 Set as End';
        setEndBtn.addEventListener('click', () => {
            toInput.value = label;
            addEndMarker(stop);
            marker.closePopup();
            showNotification(`End point set to: ${label}`);
            updateMapClickHint();
        });

        btnGroup.appendChild(setStartBtn);
        btnGroup.appendChild(setEndBtn);
        popupEl.appendChild(btnGroup);

        marker.bindPopup(popupEl, { maxWidth: 220 });

        // Prevent the bus-stop marker click from also triggering the map click handler
        // (which would set start/end to the map coordinate rather than the stop).
        marker.on('click', (e) => {
            L.DomEvent.stopPropagation(e.originalEvent);
        });

        busStopLayerGroup.addLayer(marker);
    });
}


// Route Planner Toggle functionality
function initializePlannerToggle() {
    const plannerToggle = document.getElementById('plannerToggle');
    const routePlanner = document.querySelector('.route-planner');

    plannerToggle.addEventListener('click', () => {
        const isCollapsed = routePlanner.classList.toggle('collapsed');
        plannerToggle.setAttribute('aria-expanded', !isCollapsed);
    });
}

// Sidebar functionality
const sidebar = document.getElementById('sidebar');
const menuBtn = document.getElementById('menuBtn');
const closeSidebar = document.getElementById('closeSidebar');

menuBtn.addEventListener('click', () => {
    sidebar.classList.add('active');
});

closeSidebar.addEventListener('click', () => {
    sidebar.classList.remove('active');
});

// Close sidebar when clicking outside
document.addEventListener('click', (e) => {
    if (!sidebar.contains(e.target) && !menuBtn.contains(e.target)) {
        sidebar.classList.remove('active');
    }
});

// Accessibility Modal
const accessibilityLink = document.getElementById('accessibilityLink');
const accessibilityModal = document.getElementById('accessibilityModal');
const closeModal = document.getElementById('closeModal');
const saveSettings = document.getElementById('saveSettings');

accessibilityLink.addEventListener('click', (e) => {
    e.preventDefault();
    accessibilityModal.classList.add('active');
    sidebar.classList.remove('active');
});

closeModal.addEventListener('click', () => {
    accessibilityModal.classList.remove('active');
});

// Close modal when clicking outside
accessibilityModal.addEventListener('click', (e) => {
    if (e.target === accessibilityModal) {
        accessibilityModal.classList.remove('active');
    }
});

// Bug Report Modal
const reportBugLink = document.getElementById('reportBugLink');
const bugReportModal = document.getElementById('bugReportModal');
const closeBugModal = document.getElementById('closeBugModal');
const reportBugBtn = document.getElementById('reportBugBtn');

reportBugLink.addEventListener('click', (e) => {
    e.preventDefault();
    bugReportModal.classList.add('active');
    sidebar.classList.remove('active');
});

closeBugModal.addEventListener('click', () => {
    bugReportModal.classList.remove('active');
});

// Close bug report modal when clicking outside
bugReportModal.addEventListener('click', (e) => {
    if (e.target === bugReportModal) {
        bugReportModal.classList.remove('active');
    }
});

// Handle bug report submission
reportBugBtn.addEventListener('click', () => {
    const bugType = document.getElementById('bugType').value;
    const bugDescription = document.getElementById('bugDescription').value.trim();

    if (!bugDescription) {
        showNotification('Please enter a bug description.');
        return;
    }

    // Close modal and clear form
    bugReportModal.classList.remove('active');
    document.getElementById('bugDescription').value = '';

    // Show confirmation notification
    const bugTypeText = bugType === 'ui' ? 'UI Bug' : 'Functional Bug';
    showNotification(`Thank you! Your ${bugTypeText} report has been submitted.`);
});

// Language Modal
const languageLink = document.getElementById('languageLink');
const languageModal = document.getElementById('languageModal');
const closeLanguageModal = document.getElementById('closeLanguageModal');
const saveLanguage = document.getElementById('saveLanguage');

// Translation dictionary
const translations = {
    en: {
        header: 'Lancashire Journey Planner',
        routePlanner: 'Route Planner',
        startPoint: 'Start Point:',
        startPlaceholder: 'Enter starting location',
        endPoint: 'End Point:',
        endPlaceholder: 'Enter destination',
        pathfinding: 'Pathfinding Preference:',
        fastest: 'Fastest Time',
        leastChanges: 'Least Number of Changes',
        walkingSpeed: 'Walking Speed:',
        slow: 'Slow',
        medium: 'Medium',
        fast: 'Fast',
        planRoute: 'Plan Route',
        routeInfo: 'Route Information',
        enterPoints: 'Enter your start and end points to plan your journey.',
        menuTitle: 'Menu',
        home: 'Home',
        accessibility: 'Accessibility Settings',
        languages: 'Languages',
        reportBug: 'Report Bug',
        departs: 'DEPARTS',
        arrives: 'ARRIVES',
        bus: 'Bus',
        walk: 'Walk',
        viewOnMap: '🗺️ View on Map',
        departureTime: 'Departure Time:',
        noJourneys: 'No journeys found for the selected points.',
        selectValidPoints: 'Please select valid start and end points (use the suggestions or click a suggestion).',
        planningRoute: 'Planning route…',
        routePlanningError: 'Error planning route',
        routePlanningFailed: 'Failed to plan route. See console for details.',
        clickHintStart: '🖱️ Click the map to set your start point',
        clickHintEnd: '🖱️ Click the map to set your end point',
        clickHintReset: '🖱️ Click the map to change your start point',
        mapClickNotifyStart: 'Start point set. Now click your destination on the map.',
        mapClickNotifyEnd: "End point set. Click 'Plan Route' to continue.",
        mapClickNotifyReset: 'Start point updated. Now click your destination on the map.'
    },
    zh: {
        header: '兰开夏郡旅程规划',
        routePlanner: '路线规划',
        startPoint: '起点：',
        startPlaceholder: '输入起始位置',
        endPoint: '终点：',
        endPlaceholder: '输入目的地',
        pathfinding: '路径偏好：',
        fastest: '最快时间',
        leastChanges: '最少换乘',
        walkingSpeed: '步行速度：',
        slow: '慢',
        medium: '中等',
        fast: '快',
        planRoute: '规划路线',
        routeInfo: '路线信息',
        enterPoints: '输入起点和终点以规划您的旅程。',
        menuTitle: '菜单',
        home: '主页',
        accessibility: '无障碍设置',
        languages: '语言',
        reportBug: '报告错误',
        departs: '出发',
        arrives: '到达',
        bus: '公交',
        walk: '步行',
        viewOnMap: '🗺️ 在地图上查看',
        departureTime: '出发时间：',
        noJourneys: '未找到符合所选起点和终点的路线。',
        selectValidPoints: '请选择有效的起点和终点（请使用建议列表或点击建议项）。',
        planningRoute: '正在规划路线…',
        routePlanningError: '路线规划出错',
        routePlanningFailed: '路线规划失败。请查看控制台了解详情。',
        clickHintStart: '🖱️ 点击地图设置起点',
        clickHintEnd: '🖱️ 点击地图设置终点',
        clickHintReset: '🖱️ 点击地图更改起点',
        mapClickNotifyStart: '起点已设置。请在地图上点击目的地。',
        mapClickNotifyEnd: '终点已设置。点击"规划路线"继续。',
        mapClickNotifyReset: '起点已更新。请在地图上点击目的地。'
    }
};

let currentLang = 'en';

function applyTranslations(lang) {
    const t = translations[lang];

    // Header
    document.querySelector('.header h1').textContent = t.header;

    // Route Planner
    document.querySelector('.toggle-text').textContent = t.routePlanner;
    document.querySelector('label[for="startPoint"]').textContent = t.startPoint;
    document.getElementById('startPoint').placeholder = t.startPlaceholder;
    document.querySelector('label[for="endPoint"]').textContent = t.endPoint;
    document.getElementById('endPoint').placeholder = t.endPlaceholder;
    document.querySelector('label[for="pathfinding"]').textContent = t.pathfinding;
    document.querySelector('#pathfinding option[value="fastest"]').textContent = t.fastest;
    document.querySelector('#pathfinding option[value="least-changes"]').textContent = t.leastChanges;
    document.querySelector('label[for="departureTime"]').textContent = t.departureTime;
    document.querySelector('label[for="walkingSpeed"]').textContent = t.walkingSpeed;
    document.querySelector('#walkingSpeed option[value="slow"]').textContent = t.slow;
    document.querySelector('#walkingSpeed option[value="medium"]').textContent = t.medium;
    document.querySelector('#walkingSpeed option[value="fast"]').textContent = t.fast;
    document.getElementById('planRouteBtn').textContent = t.planRoute;

    // Route Display
    document.querySelector('.route-display h3').textContent = t.routeInfo;
    const defaultText = document.querySelector('#routeContent p');
    if (defaultText && defaultText.textContent.includes('Enter your start')) {
        defaultText.textContent = t.enterPoints;
    }

    // Sidebar
    document.querySelector('.sidebar-header h2').textContent = t.menuTitle;
    document.querySelector('.sidebar-menu li:nth-child(1) a').textContent = t.home;
    document.querySelector('#accessibilityLink').textContent = t.accessibility;
    document.querySelector('#languageLink').textContent = t.languages;
    document.querySelector('#reportBugLink').textContent = t.reportBug;

    currentLang = lang;
}

languageLink.addEventListener('click', (e) => {
    e.preventDefault();
    languageModal.classList.add('active');
    sidebar.classList.remove('active');
    document.getElementById('languageSelect').value = currentLang;
});

closeLanguageModal.addEventListener('click', () => {
    languageModal.classList.remove('active');
});

languageModal.addEventListener('click', (e) => {
    if (e.target === languageModal) {
        languageModal.classList.remove('active');
    }
});

saveLanguage.addEventListener('click', () => {
    const selectedLang = document.getElementById('languageSelect').value;
    applyTranslations(selectedLang);
    localStorage.setItem('language', selectedLang);
    languageModal.classList.remove('active');
    showNotification(selectedLang === 'en' ? 'Language updated successfully!' : '语言更新成功！');
});

// Load saved language on page load
function loadLanguageSettings() {
    const savedLang = localStorage.getItem('language') || 'en';
    currentLang = savedLang;
    if (savedLang !== 'en') {
        applyTranslations(savedLang);
    }
}

// Save accessibility settings
saveSettings.addEventListener('click', () => {
    const fontSize = document.getElementById('fontSize').value;
    const highContrast = document.getElementById('highContrast').checked;
    // Apply font size - remove only the specific font size classes
    document.body.classList.remove('font-small', 'font-medium', 'font-large', 'font-extra-large');
    document.body.classList.add(`font-${fontSize}`);

    // Apply high contrast
    if (highContrast) {
        document.body.classList.add('high-contrast');
    } else {
        document.body.classList.remove('high-contrast');
    }

    // Save to localStorage
    localStorage.setItem('accessibility', JSON.stringify({
        fontSize,
        highContrast,
    }));

    accessibilityModal.classList.remove('active');

    // Show accessible notification
    showNotification('Accessibility settings saved successfully!');
});

// Load saved accessibility settings
function loadAccessibilitySettings() {
    const saved = localStorage.getItem('accessibility');
    if (saved) {
        const settings = JSON.parse(saved);

        document.getElementById('fontSize').value = settings.fontSize;
        document.getElementById('highContrast').checked = settings.highContrast;


        // Apply settings
        document.body.classList.add(`font-${settings.fontSize}`);
        if (settings.highContrast) {
            document.body.classList.add('high-contrast');
        }
    } else {
        // Default to medium font size
        document.body.classList.add('font-medium');
    }
}

// Plan Route functionality
const planRouteBtn = document.getElementById('planRouteBtn');
const routeContent = document.getElementById('routeContent');
// Layer group to hold drawn routes so we can clear them
let routeLayerGroup = null;

async function fetchStop(stop_id) {
    try {
        const res = await fetch(`${apiUrl}/stops/${encodeURIComponent(stop_id)}`);
        if (!res.ok) throw new Error('Failed to fetch stop');
        return await res.json();
    } catch (err) {
        console.error('fetchStop error:', err);
        return null;
    }
}

function clearRouteLayers() {
    if (!map) return;
    if (routeLayerGroup) {
        routeLayerGroup.clearLayers();
        routeLayerGroup.remove();
        routeLayerGroup = null;
    }
}

// Cache for route waypoints responses keyed by "route_id|direction|from_stop|to_stop"
const waypointsCache = {};

/**
 * Fetch ordered waypoints for a bus route leg from the backend.
 * Returns an array of [lat, lon] pairs, or null if unavailable.
 *
 * @param {string} routeId   - The route_id from the journey leg.
 * @param {string} direction - The direction ('outbound' or 'inbound').
 * @param {string} fromStop  - Origin stop_id of the leg.
 * @param {string} toStop    - Destination stop_id of the leg.
 */
async function fetchRouteWaypoints(routeId, direction, fromStop, toStop) {
    if (!routeId) return null;
    const cacheKey = `${routeId}|${direction || 'outbound'}|${fromStop || ''}|${toStop || ''}`;
    if (waypointsCache[cacheKey] !== undefined) return waypointsCache[cacheKey];

    try {
        let url = `${apiUrl}/routes/${encodeURIComponent(routeId)}/waypoints?direction=${encodeURIComponent(direction || 'outbound')}`;
        if (fromStop) url += `&from_stop=${encodeURIComponent(fromStop)}`;
        if (toStop)   url += `&to_stop=${encodeURIComponent(toStop)}`;
        const res = await fetch(url);
        if (!res.ok) {
            waypointsCache[cacheKey] = null;
            return null;
        }
        const data = await res.json();
        // Convert [{lat, lon}, …] → [[lat, lon], …] for Leaflet
        const coords = Array.isArray(data) && data.length > 1
            ? data.map(pt => [pt.lat, pt.lon])
            : null;
        waypointsCache[cacheKey] = coords;
        return coords;
    } catch (err) {
        console.error('fetchRouteWaypoints error:', err);
        waypointsCache[cacheKey] = null;
        return null;
    }
}

const ORS_API_KEY = 'eyJvcmciOiI1YjNjZTM1OTc4NTExMTAwMDFjZjYyNDgiLCJpZCI6IjdmZDJkOGZmNzM4MTQyMjk5ZDhhYmM2MGIxNTZiMWU4IiwiaCI6Im11cm11cjY0In0=';

// Cache for ORS route responses keyed by "lat,lon|lat,lon"
const orsCache = {};
async function routeAlongRoad(a, b, routeType = 'driving') {
    console.log("Creating route with type " + routeType);
    // a and b are [lat, lon]
    if (!a || !b) return null;
    const key = `${a[0]},${a[1]}|${b[0]},${b[1]}`;
    if (orsCache[key]) {
        console.log("Using cached route for key " + key);
        return orsCache[key];
    };
    // Map generic profile names to ORS profile names
    const profileMap = { driving: 'driving-car', walking: 'foot-walking' };
    const profile = profileMap[routeType] || 'driving-car';
    try {
        const url = `https://api.openrouteservice.org/v2/directions/${profile}?api_key=${ORS_API_KEY}&start=${a[1]},${a[0]}&end=${b[1]},${b[0]}`;
        const res = await fetch(url);
        console.log(res);
        if (!res.ok) return null;
        const body = await res.json();
        if (body && body.features && body.features.length) {
            const coords = body.features[0].geometry.coordinates.map(c => [c[1], c[0]]);
            orsCache[key] = coords;
            return coords;
        }
    } catch (err) {
        console.error('OpenRouteService request failed', err);
    }
    return null;
}

// If the user typed a stop name but didn't pick a suggestion, try to resolve it now
async function ensureSelectedFromInput(isStart = true) {
    const input = isStart ? fromInput : toInput;
    const sel = isStart ? selectedStartItem : selectedEndItem;
    if (sel) return true;
    const q = input.value && input.value.trim();
    if (!q) return false;
    try {
        const results = await getPossibleLocations(q);
        if (results && results.length) {
            const match = results.find(it => (getLabelFromItem(it) || '').trim().toLowerCase() === q.toLowerCase()) || results[0];
            if (match) {
                if (isStart) {
                    selectedStartItem = match; addStartMarker(match);
                } else {
                    selectedEndItem = match; addEndMarker(match);
                }
                return true;
            }
        }
    } catch (err) {
        console.error('ensureSelectedFromInput error', err);
    }
    return false;
}

// Distinct colours used to distinguish consecutive bus legs on the map.
const LEG_COLOURS = ['#E74C3C', '#8E44AD', '#2980B9', '#27AE60', '#F39C12', '#16A085', '#D35400', '#2C3E50'];

async function drawJourneyOnMap(journey) {
    if (!map) return;
    clearRouteLayers();
    routeLayerGroup = L.layerGroup().addTo(map);

    // Collect coordinate promises for legs that reference stop IDs
    const coordCache = {};
    async function coordsForLegEndpoint(leg, key) {
        // key is e.g. 'origin_stop_id' or 'destination_stop_id'
        const sid = leg[key];
        if (!sid) return null;
        if (coordCache[sid]) return coordCache[sid];
        // Try to find in selected items first
        if (selectedStartItem && selectedStartItem.stop_id === sid) {
            coordCache[sid] = extractLatLng(selectedStartItem);
            return coordCache[sid];
        }
        if (selectedEndItem && selectedEndItem.stop_id === sid) {
            coordCache[sid] = extractLatLng(selectedEndItem);
            return coordCache[sid];
        }
        const stop = await fetchStop(sid);
        if (stop) {
            coordCache[sid] = [parseFloat(stop.latitude), parseFloat(stop.longitude)];
            return coordCache[sid];
        }
        return null;
    }

    // Iterate legs and draw lines/markers
    const legs = journey.legs || [];
    const polylinePoints = [];
    let lastPoint = null;
    let busLegIndex = 0;
    for (let i = 0; i < legs.length; i++) {
        const leg = legs[i];
        if (leg.mode === 'walk') {
            // Draw walking transfer as dashed line.
            // Prefer waypoints provided by the OTP server (leg.waypoints), then
            // try OpenRouteService, and finally fall back to a straight line.
            const walkStyle = { color: '#3E8EDE', weight: 3, opacity: 0.8, dashArray: '8,6' };
            if (leg.waypoints && leg.waypoints.length > 1) {
                // Use the detailed walking geometry supplied by OTP.
                L.polyline(leg.waypoints, walkStyle).addTo(routeLayerGroup);
            } else {
                // Fall back to OpenRouteService or a straight line between the
                // previous bus leg's last stop and the next bus leg's first stop.
                let nextBusCoords = null;
                for (let j = i + 1; j < legs.length; j++) {
                    if ((legs[j].mode || 'bus') === 'walk') continue;
                    nextBusCoords = await coordsForLegEndpoint(legs[j], 'origin_stop_id');
                    if (nextBusCoords) break;
                }
                if (lastPoint && nextBusCoords) {
                    try {
                        console.log(`Routing walking leg from ${lastPoint} to ${nextBusCoords}`);
                        const routed = await routeAlongRoad(lastPoint, nextBusCoords, 'walking');
                        if (routed && routed.length) {
                            L.polyline(routed, walkStyle).addTo(routeLayerGroup);
                        } else {
                            L.polyline([lastPoint, nextBusCoords], walkStyle).addTo(routeLayerGroup);
                        }
                    } catch (err) {
                        L.polyline([lastPoint, nextBusCoords], walkStyle).addTo(routeLayerGroup);
                    }
                }
            }
            // don't update lastPoint here; next bus leg will set it
            continue;
        }
        // bus or default leg — assign a unique colour per leg
        const legColor = LEG_COLOURS[busLegIndex % LEG_COLOURS.length];
        busLegIndex++;
        const a = await coordsForLegEndpoint(leg, 'origin_stop_id');
        const b = await coordsForLegEndpoint(leg, 'destination_stop_id');
        if (a) polylinePoints.push(a);
        if (b) polylinePoints.push(b);
        // Draw marker for origin and destination of this leg
        if (a) {
            const popupA = `<div><strong>${leg.origin_stop_name || leg.from_stop || leg.origin_stop_id || ''}</strong>${leg.departure_time ? `<div style="color:#27AE60;font-weight:600;">Dep: ${leg.departure_time}</div>` : ''}</div>`;
            L.circleMarker(a, { radius: 6, color: legColor, fillColor: '#fff', weight: 2 }).addTo(routeLayerGroup).bindPopup(popupA);
        }
        if (b) {
            const popupB = `<div><strong>${leg.destination_stop_name || leg.to_stop || leg.destination_stop_id || ''}</strong>${leg.arrival_time ? `<div style="color:#E74C3C;font-weight:600;">Arr: ${leg.arrival_time}</div>` : ''}</div>`;
            L.circleMarker(b, { radius: 6, color: legColor, fillColor: '#fff', weight: 2 }).addTo(routeLayerGroup).bindPopup(popupB);
        }
        // Draw route for this (non-walking) leg.
        // Prefer the geometry that OTP itself used for the planned trip
        // (leg.otp_waypoints) to avoid mismatches with the route_waypoints
        // table.  Fall back to the API-server waypoints, then a straight line.
        if (a && b) {
            let waypoints = null;
            if (leg.otp_waypoints && leg.otp_waypoints.length > 1) {
                waypoints = leg.otp_waypoints;
            } else {
                waypoints = await fetchRouteWaypoints(
                    leg.route_id, leg.direction,
                    leg.origin_stop_id, leg.destination_stop_id
                );
            }
            if (waypoints && waypoints.length > 1) {
                L.polyline(waypoints, { color: legColor, weight: 4, opacity: 0.85 }).addTo(routeLayerGroup);
            } else {
                L.polyline([a, b], { color: legColor, weight: 4, opacity: 0.85 }).addTo(routeLayerGroup);
            }
            lastPoint = b;
        }

    }

    // Fit map to route
    const layers = routeLayerGroup.getLayers();
    if (layers && layers.length) {
        const group = L.featureGroup(layers);
        map.fitBounds(group.getBounds().pad(0.2));
    }
}

function renderJourneyList(journeys) {
    const t = translations[currentLang] || translations.en;
    if (!journeys || journeys.length === 0) {
        routeContent.innerHTML = `<p style="color: #e74c3c;">${t.noJourneys}</p>`;
        clearRouteLayers();
        return;
    }

    // Deduplicate journeys: skip if same departure/arrival times and route structure
    const dedupedJourneys = [];
    const seenKeys = new Set();
    for (const j of journeys) {
        const busLegs = (j.legs || []).filter(l => (l.mode || 'bus') !== 'walk');
        const firstLeg = busLegs.length > 0 ? busLegs[0] : null;
        const lastLeg = busLegs.length > 0 ? busLegs[busLegs.length - 1] : null;

        // Create a unique key based on first departure, last arrival, and route IDs
        const routeIds = (j.legs || [])
            .filter(l => (l.mode || 'bus') !== 'walk')
            .map(l => l.route_id || '')
            .join('|');
        const key = `${firstLeg?.departure_time || ''}|${lastLeg?.arrival_time || ''}|${routeIds}`;

        if (!seenKeys.has(key)) {
            seenKeys.add(key);
            dedupedJourneys.push(j);
        }
    }

    // Build a compact list with clear departure/arrival times and per-leg details
    const container = document.createElement('div');
    container.className = 'journey-list';
    container.style.display = 'flex';
    container.style.flexDirection = 'column';
    container.style.gap = '12px';
    dedupedJourneys.forEach((j, idx) => {
        const card = document.createElement('div');
        card.className = 'journey-card';
        card.style.padding = '16px';
        card.style.backgroundColor = '#ffffff';
        card.style.border = '1px solid #ddd';
        card.style.borderRadius = '8px';
        card.style.boxShadow = '0 2px 4px rgba(0,0,0,0.08)';
        card.style.transition = 'box-shadow 0.2s';
        card.addEventListener('mouseenter', () => {
            card.style.boxShadow = '0 4px 8px rgba(0,0,0,0.12)';
        });
        card.addEventListener('mouseleave', () => {
            card.style.boxShadow = '0 2px 4px rgba(0,0,0,0.08)';
        });

        // Get all non-walk legs for finding first and last
        const busLegs = j.legs && j.legs.filter(l => (l.mode || 'bus') !== 'walk') || [];
        const firstLeg = busLegs.length > 0 ? busLegs[0] : null;
        const lastLeg = busLegs.length > 0 ? busLegs[busLegs.length - 1] : null;

        // Header: route summary + departure/arrival
        const header = document.createElement('div');
        header.style.display = 'flex';
        header.style.justifyContent = 'space-between';
        header.style.alignItems = 'flex-start';
        header.style.marginBottom = '12px';
        header.style.paddingBottom = '12px';
        header.style.borderBottom = '1px solid #e8e8e8';

        const summary = document.createElement('div');
        summary.style.flex = '1';
        summary.innerHTML = `<div style="font-size:1.08em;color:#2E5090;font-weight:700;line-height:1.45;">${firstLeg && (firstLeg.origin_stop_name || firstLeg.from_stop || firstLeg.origin_stop_id) || ''}</div><div style="font-size:1em;color:#666;margin-top:0.3em;">↓</div><div style="font-size:1.08em;color:#2E5090;font-weight:700;line-height:1.45;">${lastLeg && (lastLeg.destination_stop_name || lastLeg.to_stop || lastLeg.destination_stop_id) || ''}</div>`;

        const times = document.createElement('div');
        times.style.textAlign = 'right';
        times.style.fontSize = '1em';
        times.style.lineHeight = '1.6';
        // Extract departure time ONLY from first leg, arrival time ONLY from last leg
        const depText = firstLeg && firstLeg.departure_time || '';
        const arrText = lastLeg && lastLeg.arrival_time || '';
        times.innerHTML = `<div style="margin-bottom:0.35em;"><span style="display:inline-block;font-weight:800;color:#1e8449;font-size:0.95em;text-transform:uppercase;letter-spacing:0.03em;">${t.departs}</span><div style="font-size:1.5em;font-weight:800;color:#1e8449;margin-top:0.1em;line-height:1.2;">${depText}</div></div><div style="margin-top:0.6em;"><span style="display:inline-block;font-weight:800;color:#c0392b;font-size:0.95em;text-transform:uppercase;letter-spacing:0.03em;">${t.arrives}</span><div style="font-size:1.5em;font-weight:800;color:#c0392b;margin-top:0.1em;line-height:1.2;">${arrText}</div></div>`;

        header.appendChild(summary);
        header.appendChild(times);

        card.appendChild(header);

        // Per-leg details
        const legsEl = document.createElement('div');
        legsEl.style.marginTop = '0';
        legsEl.style.fontSize = '1em';
        legsEl.style.color = '#555';

        const ul = document.createElement('ul');
        ul.style.paddingLeft = '0';
        ul.style.margin = '0';
        ul.style.listStyle = 'none';
        ul.style.display = 'flex';
        ul.style.flexDirection = 'column';
        ul.style.gap = '8px';

        // Deduplicate consecutive identical legs
        const rawLegs = j.legs || [];
        const dedupedLegs = [];
        for (let li = 0; li < rawLegs.length; li++) {
            const leg = rawLegs[li];
            // Skip if this leg is identical to the previous one
            if (dedupedLegs.length > 0) {
                const prevLeg = dedupedLegs[dedupedLegs.length - 1];
                if (prevLeg.route_id === leg.route_id &&
                    prevLeg.mode === leg.mode &&
                    prevLeg.origin_stop_id === leg.origin_stop_id &&
                    prevLeg.destination_stop_id === leg.destination_stop_id) {
                    continue; // Skip duplicate
                }
            }
            dedupedLegs.push(leg);
        }

        (dedupedLegs || []).forEach((leg) => {
            const li = document.createElement('li');
            li.style.padding = '10px 12px';
            li.style.backgroundColor = '#f8f9fa';
            li.style.borderRadius = '6px';
            li.style.borderLeft = '3px solid #2E5090';
            if ((leg.mode || 'bus') === 'walk') {
                li.style.borderLeftColor = '#95a5a6';
                li.innerHTML = `<span style="color:#7f8c8d;font-weight:700;font-size:1em;">🚶 ${t.walk}:</span> <span style="color:#555;font-size:1em;">${leg.from_stop || leg.from || ''} → ${leg.to_stop || leg.to || leg.to_stop || ''}</span>${leg.distance_km ? ` <span style="color:#999;font-size:0.92em;">(${leg.distance_km} km)</span>` : ''}`;
            } else {
                const route = leg.route_name ? `${leg.route_name}` : (leg.route_id || 'Route');
                const timesStr = `${leg.departure_time ? `<span style="color:#1e8449;font-size:1.08em;font-weight:800;">${leg.departure_time}</span>` : ''}${leg.departure_time && leg.arrival_time ? ' <span style="color:#999;">→</span> ' : ''}${leg.arrival_time ? `<span style="color:#c0392b;font-size:1.08em;font-weight:800;">${leg.arrival_time}</span>` : ''}`;
                li.innerHTML = `<div style="margin-bottom:0.35em;"><strong style="font-size:1.22em;color:#2E5090;">🚌 ${t.bus} ${route}</strong></div><div style="color:#666;font-size:1em;line-height:1.5;">${leg.origin_stop_name || leg.from_stop || leg.origin_stop_id || ''} → ${leg.destination_stop_name || leg.to_stop || leg.destination_stop_id || ''}</div><div style="margin-top:0.35em;">${timesStr}</div>`;
            }
            ul.appendChild(li);
        });
        legsEl.appendChild(ul);
        card.appendChild(legsEl);

        const btnRow = document.createElement('div');
        btnRow.style.marginTop = '12px';
        btnRow.style.paddingTop = '12px';
        btnRow.style.borderTop = '1px solid #e8e8e8';
        const viewBtn = document.createElement('button');
        viewBtn.textContent = t.viewOnMap;
        viewBtn.className = 'plan-route-btn';
        viewBtn.style.padding = '6px 14px';
        viewBtn.style.fontSize = '0.95em';
        viewBtn.style.fontWeight = '500';
        viewBtn.style.cursor = 'pointer';
        viewBtn.addEventListener('click', () => drawJourneyOnMap(j));
        btnRow.appendChild(viewBtn);
        card.appendChild(btnRow);

        container.appendChild(card);
    });

    routeContent.innerHTML = '';
    routeContent.appendChild(container);
    // Auto-show first journey on map
    drawJourneyOnMap(dedupedJourneys[0]);
}

planRouteBtn.addEventListener('click', async () => {
    const t = translations[currentLang] || translations.en;
    const pathfinding = document.getElementById('pathfinding').value;
    const walkingSpeed = document.getElementById('walkingSpeed').value;
    const departureTimeInput = document.getElementById('departureTime').value;

    // Ensure typed inputs are resolved to stops if possible
    await ensureSelectedFromInput(true);
    await ensureSelectedFromInput(false);

    // Prepare request body using stop_id if available, fallback to coords
    const body = {};
    if (selectedStartItem && selectedStartItem.stop_id) {
        body.origin_stop_id = selectedStartItem.stop_id;
    } else if (selectedStartItem) {
        const c = extractLatLng(selectedStartItem);
        if (c) {
            body.origin_lat = c[0];
            body.origin_lon = c[1];
        }
    } else if (fromInput.value) {
        // try to use typed coordinates if present as lat,lon
        const parts = fromInput.value.split(',').map(s => s.trim());
        if (parts.length === 2) { body.origin_lat = parseFloat(parts[0]); body.origin_lon = parseFloat(parts[1]); }
    }

    if (selectedEndItem && selectedEndItem.stop_id) {
        body.destination_stop_id = selectedEndItem.stop_id;
    } else if (selectedEndItem) {
        const c = extractLatLng(selectedEndItem);
        if (c) {
            body.destination_lat = c[0];
            body.destination_lon = c[1];
        }
    } else if (toInput.value) {
        const parts = toInput.value.split(',').map(s => s.trim());
        if (parts.length === 2) { body.destination_lat = parseFloat(parts[0]); body.destination_lon = parseFloat(parts[1]); }
    }

    // Add optional params (not presently used by backend but kept for future)
    body.preference = pathfinding;
    body.walking_speed = walkingSpeed;
    if (departureTimeInput) {
        body.departure_time = departureTimeInput;
    }

    // Basic validation
    if ((!body.origin_stop_id && (body.origin_lat == null || body.origin_lon == null)) || (!body.destination_stop_id && (body.destination_lat == null || body.destination_lon == null))) {
        routeContent.innerHTML = `<p style="color: #e74c3c;">${t.selectValidPoints}</p>`;
        return;
    }

    routeContent.innerHTML = `<p>${t.planningRoute}</p>`;

    try {
        const res = await fetch(`${apiUrl}/journey/plan`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            const txt = await res.text();
            throw new Error(`Server returned ${res.status}: ${txt}`);
        }
        const data = await res.json();
        // DEBUG console.log('Journey response received:', JSON.stringify(data, null, 2));
        // data.journeys is an array
        renderJourneyList(data.journeys || []);
        // Auto-collapse planner
        const routePlanner = document.querySelector('.route-planner');
        const plannerToggle = document.getElementById('plannerToggle');
        if (!routePlanner.classList.contains('collapsed')) {
            routePlanner.classList.add('collapsed');
            plannerToggle.setAttribute('aria-expanded', 'false');
        }
    } catch (err) {
        console.error('Plan route error:', err);
        routeContent.innerHTML = `<p style="color:#e74c3c;">${t.routePlanningError}: ${err.message}</p>`;
        showNotification(t.routePlanningFailed);
        clearRouteLayers();
    }
});

// Note: This function will be reimplemented for Leaflet.js in a future update
/*
function drawRouteLine() {
    const mapElement = document.getElementById('map');
    const svg = mapElement.querySelector('svg');

    if (!svg || !startMarkerEl || !endMarkerEl) return;

    // Remove existing route line
    const existingLine = svg.querySelector('#route-line');
    if (existingLine) {
        existingLine.remove();
    }

    // Get marker positions - parse without 'px' suffix
    const startX = parseFloat(startMarkerEl.style.left.replace('px', ''));
    const startY = parseFloat(startMarkerEl.style.top.replace('px', ''));
    const endX = parseFloat(endMarkerEl.style.left.replace('px', ''));
    const endY = parseFloat(endMarkerEl.style.top.replace('px', ''));

    // Create route line
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('id', 'route-line');
    line.setAttribute('x1', startX);
    line.setAttribute('y1', startY);
    line.setAttribute('x2', endX);
    line.setAttribute('y2', endY);
    line.setAttribute('stroke', '#F39C12');
    line.setAttribute('stroke-width', '4');
    line.setAttribute('stroke-dasharray', '10,5');
    line.setAttribute('opacity', '0.8');

    svg.appendChild(line);
}
*/

// (Notification helper defined above)
