// Leaflet map and markers
const apiUrl = 'https://naoma-veinal-adelina.ngrok-free.dev/api/v1';

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

// Intermediate stops state (up to MAX_STOPS entries)
const MAX_STOPS = 3;
let stopItems = [];        // array of selected location items (same shape as selectedStartItem)
let stopInputTimers = [];  // debounce timers for each stop input
let stopDwellTimes = [];   // dwell time (minutes) for each intermediate stop
let viaMarkers = [];       // Leaflet markers shown on the map for each intermediate stop input

// Maps each leg object → its Leaflet polyline layer, rebuilt by drawJourneyOnMap()
let legToPolylineMap = new Map();

// Tracks which location input box is awaiting a map click:
// null | 'from' | 'to' | { type: 'stop', idx: number }
let activeMapInput = null;

/**
 * Set or clear the active map-input state and update the visual highlight class
 * on the relevant input so it stays visually selected even while dragging the map.
 */
function setActiveMapInput(value) {
    const t = translations[currentLang] || translations.en;

    // Remove picking highlight and restore original placeholders on all inputs
    [fromInput, toInput].forEach(inp => {
        if (!inp) return;
        inp.classList.remove('map-input-picking');
        if (inp._originalPlaceholder !== undefined) {
            inp.placeholder = inp._originalPlaceholder;
            delete inp._originalPlaceholder;
        }
    });
    document.querySelectorAll('.stop-point-input').forEach(inp => {
        inp.classList.remove('map-input-picking');
        if (inp._originalPlaceholder !== undefined) {
            inp.placeholder = inp._originalPlaceholder;
            delete inp._originalPlaceholder;
        }
    });

    activeMapInput = value;

    const mapPickPlaceholder = t.mapPickPlaceholder || 'Click on the map or start typing a stop name to set this location';

    // Apply picking highlight and change placeholder on the newly active input
    if (value === 'from') {
        fromInput.classList.add('map-input-picking');
        fromInput._originalPlaceholder = fromInput.placeholder;
        fromInput.placeholder = mapPickPlaceholder;
    } else if (value === 'to') {
        toInput.classList.add('map-input-picking');
        toInput._originalPlaceholder = toInput.placeholder;
        toInput.placeholder = mapPickPlaceholder;
    } else if (value && value.type === 'stop') {
        const inp = document.querySelector(`.stop-point-input[data-idx="${value.idx}"]`);
        if (inp) {
            inp.classList.add('map-input-picking');
            inp._originalPlaceholder = inp.placeholder;
            inp.placeholder = mapPickPlaceholder;
        }
    }

    updateMapClickHint();
}

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

function addViaMarker(item, idx) {
    if (!item || !map) return;
    const coords = extractLatLng(item);
    if (!coords) return;
    if (viaMarkers[idx]) {
        map.removeLayer(viaMarkers[idx]);
        viaMarkers[idx] = null;
    }
    viaMarkers[idx] = L.circleMarker(coords, {
        radius: 8,
        fillColor: '#F39C12',
        color: '#ffffff',
        weight: 2,
        fillOpacity: 1
    }).addTo(map).bindPopup(getLabelFromItem(item));
}

function removeViaMarker(idx) {
    if (viaMarkers[idx] && map) {
        map.removeLayer(viaMarkers[idx]);
        viaMarkers[idx] = null;
    }
}
function showNotification(message) {
    if (uiSettings.disableNotifications) return;
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
    // The bottom-right map click hint has been removed.
    // This function is kept as a no-op so existing callers don't break.
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

// Live bus markers layer and refresh state
let liveBusLayerGroup = null;
let liveBusRefreshInterval = null;
let liveBusRefreshTimerControl = null;
const LIVE_BUS_REFRESH_INTERVAL_MS = 30000; // 30 seconds

// DOM selectors
let fromInput = document.getElementById('startPoint');
let toInput = document.getElementById('endPoint');

// --- Use current location buttons ---
function useCurrentLocation(isStart) {
    if (!navigator.geolocation) {
        showNotification('Geolocation is not supported by your browser.');
        return;
    }
    const btn = isStart ? document.getElementById('useLocationStart') : document.getElementById('useLocationEnd');
    if (btn) btn.disabled = true;
    navigator.geolocation.getCurrentPosition(
        (position) => {
            const latlng = { lat: position.coords.latitude, lng: position.coords.longitude };
            const item = makeCustomItem(latlng);
            const input = isStart ? fromInput : toInput;
            input.value = item.stop_name;
            if (isStart) {
                addStartMarker(item);
            } else {
                addEndMarker(item);
            }
            if (btn) btn.disabled = false;
        },
        (err) => {
            console.error('Geolocation error:', err);
            showNotification('Unable to retrieve your location.');
            if (btn) btn.disabled = false;
        },
        { enableHighAccuracy: true, timeout: 10000 }
    );
}
document.getElementById('useLocationStart').addEventListener('click', () => useCurrentLocation(true));
document.getElementById('useLocationEnd').addEventListener('click', () => useCurrentLocation(false));

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

// Hide suggestions when clicking outside and deselect active map input if click
// is not on the map or a location input (prevents accidental map picks).
document.addEventListener('click', (e) => {
    if (!fromSuggestions.contains(e.target) && e.target !== fromInput) {
        clearFromSuggestions();
    }
    if (!toSuggestions.contains(e.target) && e.target !== toInput) {
        clearToSuggestions();
    }

    // Deselect the active "click-on-map" input when the user clicks somewhere
    // that is neither the map nor one of the location input boxes.
    if (activeMapInput) {
        const mapEl = document.getElementById('map');
        const isMapClick = mapEl && mapEl.contains(e.target);
        const isInputClick = e.target === fromInput || e.target === toInput
            || e.target.classList.contains('stop-point-input');
        if (!isMapClick && !isInputClick) {
            setActiveMapInput(null);
        }
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
    setActiveMapInput('from');
    if (fromSuggestions.children.length) {
        positionFromSuggestions();
        fromSuggestions.style.display = 'block';
    }
});
toInput.addEventListener('focus', () => {
    setActiveMapInput('to');
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
    loadLanguageSettings();
    loadUiSettings();
    initializeLeafletMap();
    initializePlannerToggle();

    populateDepartureDateTimeControls();

    const timeTypeEl = document.getElementById('timeType');
    if (timeTypeEl) {
        timeTypeEl.addEventListener('change', updateDateTimeVisibilityByTimeType);
    }
    updateDateTimeVisibilityByTimeType();
});

function updateDateTimeVisibilityByTimeType() {
    const timeTypeEl = document.getElementById('timeType');
    const dateGroup = document.getElementById('departureDateGroup');
    const timeGroup = document.getElementById('departureTimeGroup');
    if (!timeTypeEl || !dateGroup || !timeGroup) return;

    const showDateTime = timeTypeEl.value !== 'now';
    dateGroup.style.display = showDateTime ? '' : 'none';
    timeGroup.style.display = showDateTime ? '' : 'none';

    if (!showDateTime) {
        const current = getCurrentDepartureSelection();
        const dateSelect = document.getElementById('departureDate');
        const timeSelect = document.getElementById('departureTime');
        if (dateSelect && current.date) dateSelect.value = current.date;
        if (timeSelect && current.time) timeSelect.value = current.time;
    }
}

function getCurrentDepartureSelection() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const roundedMins = Math.ceil(now.getMinutes() / 15) * 15;
    let dateObj = new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours(), now.getMinutes(), 0, 0);
    let hours = now.getHours();
    let mins = roundedMins;
    if (roundedMins >= 60) {
        dateObj = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 0, 0);
        hours = 0;
        mins = 0;
    }
    return {
        date: `${dateObj.getFullYear()}-${pad(dateObj.getMonth() + 1)}-${pad(dateObj.getDate())}`,
        time: `${pad(hours)}:${pad(mins)}`,
    };
}

function getExactCurrentDepartureSelection() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return {
        date: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
        time: `${pad(now.getHours())}:${pad(now.getMinutes())}`,
    };
}

function populateDepartureDateTimeControls() {
    const dateSelect = document.getElementById('departureDate');
    const timeSelect = document.getElementById('departureTime');
    if (!dateSelect || !timeSelect) return;

    const prevDate = dateSelect.value;
    const prevTime = timeSelect.value;
    const pad = (n) => String(n).padStart(2, '0');
    const locale = currentLang === 'zh' ? 'zh-CN' : 'en-GB';
    const current = getCurrentDepartureSelection();
    const now = new Date();

    // Date dropdown: today -> +7 days
    dateSelect.innerHTML = '';
    for (let i = 0; i <= 7; i++) {
        const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i);
        const value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
        const labelCore = d.toLocaleDateString(locale, { weekday: 'short', day: '2-digit', month: 'short' });
        const prefix = i === 0
            ? (currentLang === 'zh' ? '今天' : 'Today')
            : (i === 1 ? (currentLang === 'zh' ? '明天' : 'Tomorrow') : '');
        const option = document.createElement('option');
        option.value = value;
        option.textContent = prefix ? `${prefix} · ${labelCore}` : labelCore;
        dateSelect.appendChild(option);
    }
    dateSelect.value = prevDate || current.date || dateSelect.options[0]?.value || '';

    // Time dropdown: 15-min intervals
    timeSelect.innerHTML = '';
    for (let h = 0; h < 24; h++) {
        for (let m = 0; m < 60; m += 15) {
            const t = `${pad(h)}:${pad(m)}`;
            const option = document.createElement('option');
            option.value = t;
            option.textContent = t;
            timeSelect.appendChild(option);
        }
    }

    if (prevTime) {
        timeSelect.value = prevTime;
    } else {
        timeSelect.value = current.time;
    }
}


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
        zoomControl: false,
        doubleClickZoom: false
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
    const zoomHint = L.control({ position: 'bottomright' });
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
        if (!uiSettings.showMapHints) {
            hint.style.display = 'none';
            return;
        }
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
        // Apply the same zoom threshold to live bus markers
        updateLiveBusMarkers();
    });
    updateZoomHint();

    // Handle map single click: if a location input is focused and awaiting a map
    // click, fill it with the clicked coordinates and clear the active state.
    map.on('click', (e) => {
        if (!activeMapInput) return;
        const item = makeCustomItem(e.latlng);
        const label = getLabelFromItem(item);
        const t = translations[currentLang] || translations.en;
        if (activeMapInput === 'from') {
            if (fromInputTimer) { clearTimeout(fromInputTimer); fromInputTimer = null; }
            fromInput.value = label;
            clearFromSuggestions();
            addStartMarker(item);
            showNotification(t.mapClickNotifyStart);
        } else if (activeMapInput === 'to') {
            if (toInputTimer) { clearTimeout(toInputTimer); toInputTimer = null; }
            toInput.value = label;
            clearToSuggestions();
            addEndMarker(item);
            showNotification(t.mapClickNotifyEnd);
        } else if (activeMapInput && activeMapInput.type === 'stop') {
            const idx = activeMapInput.idx;
            stopItems[idx] = item;
            const inp = document.querySelector(`.stop-point-input[data-idx="${idx}"]`);
            if (inp) inp.value = label;
            clearStopSuggestions(idx);
            addViaMarker(item, idx);
            showNotification(t.mapClickNotifyStop || 'Via stop set.');
        }
        setActiveMapInput(null);
    });

    // Start live bus tracking with 30-second auto-refresh if enabled
    if (uiSettings.showLiveBuses) {
        startLiveBusRefresh();
    }

    // Ensure Leaflet tiles are sized after layout settles
    setTimeout(() => {
        if (map) map.invalidateSize();
    }, 0);
}

/**
 * Fetch bus stops within the current map bounds and render them as
 * clickable Leaflet markers.  Only active when zoom >= BUS_STOP_ZOOM_THRESHOLD.
 */
async function fetchStopsForBoundsWithFallback(bounds) {
    const params = new URLSearchParams({
        min_lat: bounds.getSouth(),
        max_lat: bounds.getNorth(),
        min_lon: bounds.getWest(),
        max_lon: bounds.getEast(),
        limit: 200,
    });

    // Primary: dedicated bounds endpoint
    try {
        const res = await fetch(`${apiUrl}/stops/bounds?${params}`);
        if (res.ok) {
            const rows = await res.json();
            if (Array.isArray(rows)) return rows;
        }
    } catch (err) {
        console.warn('Bounds stop lookup failed, trying fallback:', err);
    }

    // Fallback: page through /stops and filter client-side by viewport.
    // This keeps stop icons available even if /stops/bounds fails.
    const inBounds = [];
    const pageSize = 500;
    const maxPages = 12; // hard cap to avoid excessive requests

    for (let page = 0; page < maxPages; page++) {
        const offset = page * pageSize;
        try {
            const res = await fetch(`${apiUrl}/stops?limit=${pageSize}&offset=${offset}`);
            if (!res.ok) break;

            const rows = await res.json();
            if (!Array.isArray(rows) || rows.length === 0) break;

            rows.forEach((stop) => {
                const lat = parseFloat(stop.latitude);
                const lon = parseFloat(stop.longitude);
                if (isNaN(lat) || isNaN(lon)) return;
                if (
                    lat >= bounds.getSouth() &&
                    lat <= bounds.getNorth() &&
                    lon >= bounds.getWest() &&
                    lon <= bounds.getEast()
                ) {
                    inBounds.push(stop);
                }
            });

            if (rows.length < pageSize) break;
        } catch (err) {
            console.error('Fallback stop pagination failed:', err);
            break;
        }
    }

    // Deduplicate rail stops client-side: only keep primary station nodes
    // (9100 prefix).  Entrances (2590/9200 etc.) are filtered out.
    const output = inBounds.filter((stop) => {
        if ((stop.stop_type || '').toLowerCase() !== 'rail') return true;
        return String(stop.stop_id || '').startsWith('9100');
    });
    return output;
}

async function updateBusStopMarkers() {
    if (!map) return;

    if (!uiSettings.showBusStops) {
        if (busStopLayerGroup) {
            busStopLayerGroup.clearLayers();
        }
        return;
    }

    // Below threshold – remove any existing stop markers and bail out
    if (map.getZoom() < BUS_STOP_ZOOM_THRESHOLD) {
        if (busStopLayerGroup) {
            busStopLayerGroup.clearLayers();
        }
        return;
    }

    let stops = [];
    try {
        const bounds = map.getBounds();
        stops = await fetchStopsForBoundsWithFallback(bounds);
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

        const isRailStop = (stop.stop_type || '').toLowerCase() === 'rail';
        const iconLabel = isRailStop ? 'R' : 'B';
        const iconColor = isRailStop ? '#7B2CBF' : '#2E5090';
        const iconAria = isRailStop ? 'Rail stop' : 'Bus stop';

        // Custom stop icon (DivIcon so it works without external images)
        const icon = L.divIcon({
            className: isRailStop ? 'rail-stop-icon' : 'bus-stop-icon',
            html: `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32" role="img" aria-label="${iconAria}">
                <circle cx="16" cy="16" r="15" fill="${iconColor}" stroke="white" stroke-width="2.5"/>
                <text x="16" y="21" font-family="Arial,sans-serif" font-size="15" font-weight="bold" fill="white" text-anchor="middle">${iconLabel}</text>
            </svg>`,
            iconSize: [32, 32],
            iconAnchor: [16, 16],
            popupAnchor: [0, -18],
        });

        const label = stop.stop_name + (stop.locality ? ` (${stop.locality})` : '');
        const marker = L.marker([lat, lon], {
            icon,
            title: label,
            alt: `${isRailStop ? 'Rail' : 'Bus'} stop: ${label}`,
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

        const viewDeparturesBtn = document.createElement('button');
        viewDeparturesBtn.className = 'bus-stop-popup-btn';
        viewDeparturesBtn.setAttribute('aria-label', `View departures for ${label}`);
        const t = translations[currentLang] || translations.en;
        viewDeparturesBtn.textContent = `🕒 ${t.viewDepartures || 'View Departures'}`;
        viewDeparturesBtn.addEventListener('click', async () => {
            marker.closePopup();
            await openDeparturesOverlay(stop);
        });

        btnGroup.appendChild(setStartBtn);
        btnGroup.appendChild(setEndBtn);
        btnGroup.appendChild(viewDeparturesBtn);
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


/**
 * Fetch live bus positions from the backend SIRI proxy and render them on the
 * map as animated bus icons.  Each bus popup shows the vehicle ID and the
 * line/route it is operating.
 */
async function updateLiveBusMarkers() {
    if (!map) return;

    if (!uiSettings.showLiveBuses) {
        if (liveBusLayerGroup) {
            liveBusLayerGroup.clearLayers();
        }
        return;
    }

    // Only show live bus icons at the same zoom level as bus stop icons
    if (map.getZoom() < BUS_STOP_ZOOM_THRESHOLD) {
        if (liveBusLayerGroup) {
            liveBusLayerGroup.clearLayers();
        }
        return;
    }

    let data;
    try {
        const res = await fetch(`${apiUrl}/disruptions/live/vehicles`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        data = await res.json();
    } catch (err) {
        console.warn('Failed to fetch live bus positions:', err);
        return;
    }

    const vehicles = (data && data.vehicles) ? data.vehicles : [];

    if (!liveBusLayerGroup) {
        liveBusLayerGroup = L.layerGroup().addTo(map);
    } else {
        liveBusLayerGroup.clearLayers();
    }

    vehicles.forEach((bus) => {
        const lat = bus.latitude;
        const lon = bus.longitude;
        if (isNaN(lat) || isNaN(lon)) return;

        const rotation = bus.bearing || 0;
        const icon = L.divIcon({
            className: 'live-bus-icon',
            html: `<div class="live-bus-icon__inner" style="transform:rotate(${rotation}deg)" aria-hidden="true">
                <svg xmlns="http://www.w3.org/2000/svg" width="34" height="34" viewBox="0 0 34 34" role="img" aria-label="Live bus">
                    <circle cx="17" cy="17" r="16" fill="#F39C12" stroke="white" stroke-width="2.5"/>
                    <text x="17" y="14" font-family="Arial,sans-serif" font-size="8" font-weight="bold" fill="white" text-anchor="middle">BUS</text>
                    <rect x="9" y="15" width="16" height="9" rx="2" fill="white" opacity="0.9"/>
                    <rect x="10" y="16" width="6" height="4" rx="1" fill="#F39C12"/>
                    <rect x="18" y="16" width="6" height="4" rx="1" fill="#F39C12"/>
                    <circle cx="12" cy="25" r="1.5" fill="white"/>
                    <circle cx="22" cy="25" r="1.5" fill="white"/>
                </svg>
            </div>`,
            iconSize: [34, 34],
            iconAnchor: [17, 17],
            popupAnchor: [0, -20],
        });

        const lineName = bus.line_name || bus.line_ref || 'Unknown';
        const marker = L.marker([lat, lon], {
            icon,
            title: `Bus ${bus.vehicle_id} – Line ${lineName}`,
            alt: `Live bus: vehicle ${bus.vehicle_id} on line ${lineName}`,
            zIndexOffset: 500,
        });

        const popupEl = document.createElement('div');
        popupEl.className = 'live-bus-popup';

        const titleEl = document.createElement('strong');
        titleEl.textContent = `Live Bus`;
        popupEl.appendChild(titleEl);

        const lineEl = document.createElement('p');
        lineEl.className = 'live-bus-popup__line';
        lineEl.textContent = `Line: ${lineName}`;
        popupEl.appendChild(lineEl);

        const vehicleEl = document.createElement('p');
        vehicleEl.className = 'live-bus-popup__vehicle';
        vehicleEl.textContent = `Vehicle: ${bus.vehicle_id}`;
        popupEl.appendChild(vehicleEl);

        const operatorEl = document.createElement('p');
        operatorEl.className = 'live-bus-popup__operator';
        operatorEl.textContent = `Operator: ${bus.operator}`;
        popupEl.appendChild(operatorEl);

        marker.bindPopup(popupEl, { maxWidth: 220 });

        marker.on('click', (e) => {
            L.DomEvent.stopPropagation(e.originalEvent);
        });

        liveBusLayerGroup.addLayer(marker);
    });
}


/**
 * Start the live-bus auto-refresh loop and add the countdown timer control to
 * the top-left corner of the map.  Calling this more than once is safe – the
 * existing interval and control will be cleared first.
 */
function startLiveBusRefresh() {
    if (!map) return;

    // Clear any previous interval
    if (liveBusRefreshInterval) {
        clearInterval(liveBusRefreshInterval);
        liveBusRefreshInterval = null;
    }

    // Remove previous timer control if present
    if (liveBusRefreshTimerControl) {
        liveBusRefreshTimerControl.remove();
        liveBusRefreshTimerControl = null;
    }

    const INTERVAL_SEC = LIVE_BUS_REFRESH_INTERVAL_MS / 1000;
    let secondsLeft = INTERVAL_SEC;

    // Leaflet control that shows a countdown ring
    liveBusRefreshTimerControl = L.control({ position: 'topleft' });
    liveBusRefreshTimerControl.onAdd = function () {
        const container = L.DomUtil.create('div', 'live-bus-refresh-timer');
        container.setAttribute('aria-live', 'polite');
        container.setAttribute('aria-label', 'Live bus refresh timer');
        container.innerHTML = _buildTimerHTML(INTERVAL_SEC, INTERVAL_SEC);
        return container;
    };
    liveBusRefreshTimerControl.addTo(map);

    function tick() {
        secondsLeft -= 1;
        const timerEl = document.querySelector('.live-bus-refresh-timer');
        if (timerEl) {
            timerEl.innerHTML = _buildTimerHTML(secondsLeft, INTERVAL_SEC);
        }
        if (secondsLeft <= 0) {
            secondsLeft = INTERVAL_SEC;
            updateLiveBusMarkers();
        }
    }

    // Initial fetch immediately
    updateLiveBusMarkers();

    // Countdown tick every second
    liveBusRefreshInterval = setInterval(tick, 1000);
}

function stopLiveBusRefresh() {
    if (liveBusRefreshInterval) {
        clearInterval(liveBusRefreshInterval);
        liveBusRefreshInterval = null;
    }

    if (liveBusRefreshTimerControl) {
        liveBusRefreshTimerControl.remove();
        liveBusRefreshTimerControl = null;
    }

    if (liveBusLayerGroup) {
        liveBusLayerGroup.clearLayers();
    }
}


/** Build the SVG countdown ring HTML for the refresh timer control. */
function _buildTimerHTML(secondsLeft, total) {
    const RADIUS = 16;
    const CIRCUMFERENCE = 2 * Math.PI * RADIUS;
    const fraction = Math.max(0, secondsLeft / total);
    const dashOffset = CIRCUMFERENCE * (1 - fraction);
    const displaySec = Math.max(0, secondsLeft);

    return `<div class="live-bus-refresh-timer__inner" title="Live buses refresh in ${displaySec}s">
        <svg width="44" height="44" viewBox="0 0 44 44" role="img" aria-label="Refresh in ${displaySec} seconds">
            <circle cx="22" cy="22" r="${RADIUS}" fill="var(--primary-color)" stroke="rgba(255,255,255,0.25)" stroke-width="3"/>
            <circle cx="22" cy="22" r="${RADIUS}" fill="none" stroke="var(--secondary-color)" stroke-width="3"
                stroke-dasharray="${CIRCUMFERENCE.toFixed(2)}"
                stroke-dashoffset="${dashOffset.toFixed(2)}"
                stroke-linecap="round"
                transform="rotate(-90 22 22)"/>
            <text x="22" y="26" font-family="Arial,sans-serif" font-size="11" font-weight="bold"
                fill="white" text-anchor="middle">${displaySec}</text>
        </svg>
        <span class="live-bus-refresh-timer__label" role="img" aria-label="Bus">🚌</span>
    </div>`;
}

// Route Planner Toggle functionality
function initializePlannerToggle() {
    const plannerToggle = document.getElementById('plannerToggle');
    const routePlanner = document.querySelector('.route-planner');

    plannerToggle.addEventListener('click', () => {
        const isCollapsed = routePlanner.classList.toggle('collapsed');
        plannerToggle.setAttribute('aria-expanded', !isCollapsed);
        setTimeout(() => {
            if (map) map.invalidateSize();
        }, 320);
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

// App Settings Modal
const settingsLink = document.getElementById('settingsLink');
const settingsModal = document.getElementById('settingsModal');
const closeSettingsModal = document.getElementById('closeSettingsModal');
const saveUiSettingsBtn = document.getElementById('saveUiSettings');
const settingsShowWeatherInput = document.getElementById('settingsShowWeather');
const settingsShowMapHintsInput = document.getElementById('settingsShowMapHints');
const settingsDarkMapInput = document.getElementById('settingsDarkMap');
const settingsShowLiveBusesInput = document.getElementById('settingsShowLiveBuses');
const settingsShowBusStopsInput = document.getElementById('settingsShowBusStops');
const settingsDisableNotificationsInput = document.getElementById('settingsDisableNotifications');
const fontSizeInput = document.getElementById('fontSize');
const highContrastInput = document.getElementById('highContrast');

const defaultUiSettings = {
    showWeather: true,
    showMapHints: true,
    darkMap: false,
    showLiveBuses: true,
    showBusStops: true,
    disableNotifications: false,
};

let uiSettings = { ...defaultUiSettings };

function applyUiSettings() {
    const weatherWidget = document.getElementById('weatherWidget');
    if (weatherWidget) {
        weatherWidget.style.display = uiSettings.showWeather ? '' : 'none';
    }

    document.body.classList.toggle('hide-map-hints', !uiSettings.showMapHints);
    document.body.classList.toggle('map-night-mode', !!uiSettings.darkMap);

    if (map) {
        if (uiSettings.showLiveBuses) {
            startLiveBusRefresh();
        } else {
            stopLiveBusRefresh();
        }
        updateBusStopMarkers();
    }

    updateMapClickHint();
}

function loadUiSettings() {
    const raw = localStorage.getItem('ui_settings');
    if (raw) {
        try {
            const parsed = JSON.parse(raw);
            uiSettings = {
                ...defaultUiSettings,
                ...parsed,
            };
        } catch (err) {
            uiSettings = { ...defaultUiSettings };
        }
    } else {
        uiSettings = { ...defaultUiSettings };
    }

    if (settingsShowWeatherInput) settingsShowWeatherInput.checked = !!uiSettings.showWeather;
    if (settingsShowMapHintsInput) settingsShowMapHintsInput.checked = !!uiSettings.showMapHints;
    if (settingsDarkMapInput) settingsDarkMapInput.checked = !!uiSettings.darkMap;
    if (settingsShowLiveBusesInput) settingsShowLiveBusesInput.checked = !!uiSettings.showLiveBuses;
    if (settingsShowBusStopsInput) settingsShowBusStopsInput.checked = !!uiSettings.showBusStops;
    if (settingsDisableNotificationsInput) settingsDisableNotificationsInput.checked = !!uiSettings.disableNotifications;
    applyUiSettings();
}

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
        languages: 'Languages',
        settings: 'Settings',
        reportBug: 'Report Bug',
        settingsTitle: 'Settings',
        settingsSectionMap: 'Map Settings',
        settingsSectionNotifications: 'Notification Settings',
        settingsSectionAccessibility: 'Accessibility Settings',
        settingsShowWeather: 'Show weather icon',
        settingsShowMapHints: 'Show map helper pop-ups',
        settingsDarkMap: 'Dark mode map tint',
        settingsShowLiveBuses: 'Show live bus locations',
        settingsShowBusStops: 'Show bus stops',
        settingsDisableNotifications: 'Disable notification messages',
        fontSize: 'Font Size:',
        highContrast: 'High Contrast Mode:',
        settingsSave: 'Save Settings',
        settingsSaved: 'Settings updated successfully!',
        departs: 'DEPARTS',
        arrives: 'ARRIVES',
        bus: 'Bus',
        train: 'Train',
        tram: 'Tram',
        walk: 'Walk',
        serviceTo: 'Service to',
        viewOnMap: '🗺️ View on Map',
        dateTime: 'Date & Time:',
        dateLabel: 'Date:',
        timeLabel: 'Time:',
        timeTypeLabel: 'Time Type:',
        now: 'Now',
        departAfter: 'Depart After',
        arriveBefore: 'Arrive Before',
        noJourneys: 'No journeys found for the selected points.',
        selectValidPoints: 'Please select valid start and end points (use the suggestions or click a suggestion).',
        planningRoute: 'Planning route…',
        routePlanningError: 'Error planning route',
        routePlanningFailed: 'Failed to plan route. See console for details.',
        mapPickPlaceholder: 'Click on the map or start typing a stop name to set this location',
        mapClickNotifyStart: 'Start point set.',
        mapClickNotifyEnd: "End point set. Click 'Plan Route' to continue.",
        mapClickNotifyReset: 'Start point updated.',
        mapClickNotifyStop: 'Via stop set.',
        stopDwellLabel: 'Stop time (min):',
        viewDepartures: 'View Departures',
        departuresTitle: 'Departures (next 24 hours)',
        departuresLoading: 'Loading departures…',
        departuresNone: 'No scheduled departures found in the next 24 hours.',
        departuresError: 'Could not load departures for this stop.',
        departuresToPrefix: 'To',
        arrivesLateWarning: '⚠️ Arrives {mins} min after target time',
        alreadyDepartedWarning: '⚠️ This journey has already departed'
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
        languages: '语言',
        settings: '设置',
        reportBug: '报告错误',
        settingsTitle: '设置',
        settingsSectionMap: '地图设置',
        settingsSectionNotifications: '通知设置',
        settingsSectionAccessibility: '无障碍设置',
        settingsShowWeather: '显示天气图标',
        settingsShowMapHints: '显示地图提示弹窗',
        settingsDarkMap: '地图夜间深色',
        settingsShowLiveBuses: '显示实时公交位置',
        settingsShowBusStops: '显示公交站点',
        settingsDisableNotifications: '禁用通知消息',
        fontSize: '字体大小：',
        highContrast: '高对比度模式：',
        settingsSave: '保存设置',
        settingsSaved: '设置更新成功！',
        departs: '出发',
        arrives: '到达',
        bus: '公交',
        train: '火车',
        tram: '电车',
        walk: '步行',
        serviceTo: '开往',
        viewOnMap: '🗺️ 在地图上查看',
        dateTime: '日期和时间：',
        dateLabel: '日期：',
        timeLabel: '时间：',
        timeTypeLabel: '时间类型：',
        now: '现在',
        departAfter: '出发时间不早于',
        arriveBefore: '到达时间不晚于',
        noJourneys: '未找到符合所选起点和终点的路线。',
        selectValidPoints: '请选择有效的起点和终点（请使用建议列表或点击建议项）。',
        planningRoute: '正在规划路线…',
        routePlanningError: '路线规划出错',
        routePlanningFailed: '路线规划失败。请查看控制台了解详情。',
        mapPickPlaceholder: '点击地图设置此位置',
        mapClickNotifyStart: '起点已设置。',
        mapClickNotifyEnd: '终点已设置。点击"规划路线"继续。',
        mapClickNotifyReset: '起点已更新。',
        mapClickNotifyStop: '途经站点已设置。',
        stopDwellLabel: '停留时间（分钟）：',
        viewDepartures: '查看发车',
        departuresTitle: '发车信息（未来24小时）',
        departuresLoading: '正在加载发车信息…',
        departuresNone: '未来24小时内没有计划发车。',
        departuresError: '无法加载该站点的发车信息。',
        departuresToPrefix: '开往',
        arrivesLateWarning: '⚠️ 比预定到达时间晚 {mins} 分钟',
        alreadyDepartedWarning: '⚠️ 此行程已出发'
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
    const dateLabelEl = document.querySelector('label[for="departureDate"]');
    if (dateLabelEl) dateLabelEl.textContent = t.dateLabel || t.dateTime;
    const timeLabelEl = document.querySelector('label[for="departureTime"]');
    if (timeLabelEl) timeLabelEl.textContent = t.timeLabel || 'Time:';
    document.querySelector('label[for="timeType"]').textContent = t.timeTypeLabel;
    const nowOption = document.querySelector('#timeType option[value="now"]');
    if (nowOption) nowOption.textContent = t.now || 'Now';
    document.querySelector('#timeType option[value="depart-after"]').textContent = t.departAfter;
    document.querySelector('#timeType option[value="arrive-before"]').textContent = t.arriveBefore;
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
    document.querySelector('#languageLink').textContent = t.languages;
    document.querySelector('#settingsLink').textContent = t.settings;
    document.querySelector('#reportBugLink').textContent = t.reportBug;

    // Settings modal
    const settingsTitle = document.querySelector('#settingsModal .modal-header h2');
    if (settingsTitle) settingsTitle.textContent = t.settingsTitle;
    const sectionMap = document.getElementById('settingsSectionMap');
    if (sectionMap) sectionMap.textContent = t.settingsSectionMap;
    const sectionNotifications = document.getElementById('settingsSectionNotifications');
    if (sectionNotifications) sectionNotifications.textContent = t.settingsSectionNotifications;
    const sectionAccessibility = document.getElementById('settingsSectionAccessibility');
    if (sectionAccessibility) sectionAccessibility.textContent = t.settingsSectionAccessibility;
    const weatherLabel = document.querySelector('label[for="settingsShowWeather"]');
    if (weatherLabel) weatherLabel.textContent = t.settingsShowWeather;
    const hintsLabel = document.querySelector('label[for="settingsShowMapHints"]');
    if (hintsLabel) hintsLabel.textContent = t.settingsShowMapHints;
    const darkMapLabel = document.querySelector('label[for="settingsDarkMap"]');
    if (darkMapLabel) darkMapLabel.textContent = t.settingsDarkMap;
    const showLiveBusesLabel = document.querySelector('label[for="settingsShowLiveBuses"]');
    if (showLiveBusesLabel) showLiveBusesLabel.textContent = t.settingsShowLiveBuses;
    const showBusStopsLabel = document.querySelector('label[for="settingsShowBusStops"]');
    if (showBusStopsLabel) showBusStopsLabel.textContent = t.settingsShowBusStops;
    const disableNotificationsLabel = document.querySelector('label[for="settingsDisableNotifications"]');
    if (disableNotificationsLabel) disableNotificationsLabel.textContent = t.settingsDisableNotifications;
    const fontSizeLabel = document.querySelector('label[for="fontSize"]');
    if (fontSizeLabel) fontSizeLabel.textContent = t.fontSize;
    const highContrastLabel = document.querySelector('label[for="highContrast"]');
    if (highContrastLabel) highContrastLabel.textContent = t.highContrast;
    const settingsSaveBtn = document.getElementById('saveUiSettings');
    if (settingsSaveBtn) settingsSaveBtn.textContent = t.settingsSave;

    currentLang = lang;
    populateDepartureDateTimeControls();
    updateDateTimeVisibilityByTimeType();
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

settingsLink.addEventListener('click', (e) => {
    e.preventDefault();
    settingsShowWeatherInput.checked = !!uiSettings.showWeather;
    settingsShowMapHintsInput.checked = !!uiSettings.showMapHints;
    if (settingsDarkMapInput) settingsDarkMapInput.checked = !!uiSettings.darkMap;
    if (settingsShowLiveBusesInput) settingsShowLiveBusesInput.checked = !!uiSettings.showLiveBuses;
    if (settingsShowBusStopsInput) settingsShowBusStopsInput.checked = !!uiSettings.showBusStops;
    if (settingsDisableNotificationsInput) settingsDisableNotificationsInput.checked = !!uiSettings.disableNotifications;
    settingsModal.classList.add('active');
    sidebar.classList.remove('active');
});

closeSettingsModal.addEventListener('click', () => {
    settingsModal.classList.remove('active');
});

settingsModal.addEventListener('click', (e) => {
    if (e.target === settingsModal) {
        settingsModal.classList.remove('active');
    }
});

saveUiSettingsBtn.addEventListener('click', () => {
    uiSettings.showWeather = !!settingsShowWeatherInput.checked;
    uiSettings.showMapHints = !!settingsShowMapHintsInput.checked;
    uiSettings.darkMap = !!(settingsDarkMapInput && settingsDarkMapInput.checked);
    uiSettings.showLiveBuses = !!(settingsShowLiveBusesInput && settingsShowLiveBusesInput.checked);
    uiSettings.showBusStops = !!(settingsShowBusStopsInput && settingsShowBusStopsInput.checked);
    uiSettings.disableNotifications = !!(settingsDisableNotificationsInput && settingsDisableNotificationsInput.checked);
    localStorage.setItem('ui_settings', JSON.stringify(uiSettings));
    saveAccessibilitySettings();
    applyUiSettings();
    settingsModal.classList.remove('active');
    const t = translations[currentLang] || translations.en;
    showNotification(t.settingsSaved || 'Settings updated successfully!');
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
function saveAccessibilitySettings() {
    const fontSize = fontSizeInput ? fontSizeInput.value : 'medium';
    const highContrast = highContrastInput ? highContrastInput.checked : false;
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
}

// Load saved accessibility settings
function loadAccessibilitySettings() {
    const saved = localStorage.getItem('accessibility');
    document.body.classList.remove('font-small', 'font-medium', 'font-large', 'font-extra-large');
    document.body.classList.remove('high-contrast');
    if (saved) {
        let settings = {};
        try {
            settings = JSON.parse(saved || '{}');
        } catch (err) {
            settings = {};
        }

        if (fontSizeInput && settings.fontSize) fontSizeInput.value = settings.fontSize;
        if (highContrastInput) highContrastInput.checked = !!settings.highContrast;


        // Apply settings
        document.body.classList.add(`font-${settings.fontSize || 'medium'}`);
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
const routeDisplay = document.getElementById('routeDisplay');
// Layer group to hold drawn routes so we can clear them
let routeLayerGroup = null;

let departuresOverlay = null;
let departuresOverlayTitle = null;
let departuresOverlayBody = null;

function ensureDeparturesOverlay() {
    if (departuresOverlay) return departuresOverlay;
    if (!routeDisplay) return null;

    departuresOverlay = document.createElement('section');
    departuresOverlay.className = 'route-display-overlay';
    departuresOverlay.setAttribute('aria-live', 'polite');

    const header = document.createElement('div');
    header.className = 'route-display-overlay-header';

    departuresOverlayTitle = document.createElement('div');
    departuresOverlayTitle.className = 'route-display-overlay-title';

    const closeBtn = document.createElement('button');
    closeBtn.className = 'route-display-overlay-close';
    closeBtn.setAttribute('type', 'button');
    closeBtn.setAttribute('aria-label', 'Close departures panel');
    closeBtn.innerHTML = '&times;';
    closeBtn.addEventListener('click', closeDeparturesOverlay);

    header.appendChild(departuresOverlayTitle);
    header.appendChild(closeBtn);

    departuresOverlayBody = document.createElement('div');
    departuresOverlayBody.className = 'route-display-overlay-body';

    departuresOverlay.appendChild(header);
    departuresOverlay.appendChild(departuresOverlayBody);
    routeDisplay.appendChild(departuresOverlay);

    return departuresOverlay;
}

function closeDeparturesOverlay() {
    if (!departuresOverlay) return;
    departuresOverlay.classList.remove('active');
    if (routeDisplay) {
        routeDisplay.classList.remove('showing-departures');
    }
}

function formatDepartureDateTime(value) {
    if (!value) return '';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    return d.toLocaleString([], {
        weekday: 'short',
        hour: '2-digit',
        minute: '2-digit',
        day: '2-digit',
        month: 'short',
    });
}

async function openDeparturesOverlay(stop) {
    const overlay = ensureDeparturesOverlay();
    if (!overlay || !departuresOverlayBody || !departuresOverlayTitle) return;

    const t = translations[currentLang] || translations.en;
    const stopName = (stop && (stop.stop_name || stop.name || stop.label)) || 'Stop';
    const stopId = stop && stop.stop_id;

    departuresOverlayTitle.textContent = `${t.departuresTitle || 'Departures (next 24 hours)'} · ${stopName}`;
    departuresOverlayBody.innerHTML = `<p>${t.departuresLoading || 'Loading departures…'}</p>`;
    overlay.classList.add('active');
    if (routeDisplay) {
        routeDisplay.classList.add('showing-departures');
    }

    if (!stopId) {
        departuresOverlayBody.innerHTML = `<p>${t.departuresError || 'Could not load departures for this stop.'}</p>`;
        return;
    }

    try {
        const res = await fetch(`${apiUrl}/stops/${encodeURIComponent(stopId)}/departures`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const departures = Array.isArray(data.departures) ? data.departures : [];

        if (!departures.length) {
            departuresOverlayBody.innerHTML = `<p>${t.departuresNone || 'No scheduled departures found in the next 24 hours.'}</p>`;
            return;
        }

        const list = document.createElement('ul');
        list.className = 'departures-list';

        departures.forEach((dep) => {
            const item = document.createElement('li');
            item.className = 'departures-item';

            const route = document.createElement('div');
            route.className = 'departures-item-route';
            route.textContent = `${dep.route_name || dep.route_id || 'Route'} · ${dep.departure_time || ''}`;

            const meta = document.createElement('div');
            meta.className = 'departures-item-meta';
            const when = formatDepartureDateTime(dep.departure_datetime);
            const operator = dep.operator ? ` · ${dep.operator}` : '';
            const destinationText = dep.final_destination_name
                ? ` · ${(t.departuresToPrefix || 'To')} ${dep.final_destination_name}`
                : '';
            meta.textContent = `${when}${destinationText}${operator}`;

            item.appendChild(route);
            item.appendChild(meta);
            list.appendChild(item);
        });

        departuresOverlayBody.innerHTML = '';
        departuresOverlayBody.appendChild(list);
    } catch (err) {
        console.error('Failed to load departures:', err);
        departuresOverlayBody.innerHTML = `<p>${t.departuresError || 'Could not load departures for this stop.'}</p>`;
    }
}

window.addEventListener('resize', () => {
    if (map) map.invalidateSize();
});

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
// Cache for route stops-between responses keyed by
// "route_id|direction|from_stop|to_stop|departure_time"
const routeStopsBetweenCache = {};

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
        if (toStop) url += `&to_stop=${encodeURIComponent(toStop)}`;
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

/**
 * Fetch ordered stop calls between origin/destination for one route leg.
 * Returns an object with { stops: [...] } or null if unavailable.
 */
async function fetchRouteStopsBetween(routeId, direction, fromStop, toStop, departureTime) {
    if (!routeId || !fromStop || !toStop) return null;
    const cacheKey = `${routeId}|${direction || ''}|${fromStop}|${toStop}|${departureTime || ''}`;
    if (routeStopsBetweenCache[cacheKey] !== undefined) return routeStopsBetweenCache[cacheKey];

    try {
        let url = `${apiUrl}/routes/${encodeURIComponent(routeId)}/stops-between`;
        url += `?from_stop=${encodeURIComponent(fromStop)}`;
        url += `&to_stop=${encodeURIComponent(toStop)}`;
        if (direction) url += `&direction=${encodeURIComponent(direction)}`;
        if (departureTime) url += `&departure_time=${encodeURIComponent(departureTime)}`;

        const res = await fetch(url);
        if (!res.ok) {
            routeStopsBetweenCache[cacheKey] = null;
            return null;
        }
        const data = await res.json();
        routeStopsBetweenCache[cacheKey] = data;
        return data;
    } catch (err) {
        console.error('fetchRouteStopsBetween error:', err);
        routeStopsBetweenCache[cacheKey] = null;
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

/**
 * Ensure a stop at a given index is resolved (typed text → stop item).
 * @param {number} idx - Index into stopItems / stop input elements
 */
async function ensureStopResolved(idx) {
    if (stopItems[idx]) return true;
    const inputs = document.querySelectorAll('.stop-point-input');
    const input = inputs[idx];
    if (!input) return false;
    const q = input.value && input.value.trim();
    if (!q) return false;
    try {
        const results = await getPossibleLocations(q);
        if (results && results.length) {
            const match = results.find(it => (getLabelFromItem(it) || '').trim().toLowerCase() === q.toLowerCase()) || results[0];
            if (match) {
                stopItems[idx] = match;
                return true;
            }
        }
    } catch (err) {
        console.error('ensureStopResolved error', err);
    }
    return false;
}

/**
 * Refresh the Add Stop button state based on current stop count.
 */
function refreshAddStopBtn() {
    const btn = document.getElementById('addStopBtn');
    if (!btn) return;
    const count = document.querySelectorAll('.stop-input-row').length;
    btn.disabled = count >= MAX_STOPS;
}

/**
 * Create and append a new intermediate stop input row.
 */
function addStopRow() {
    const container = document.getElementById('stopsContainer');
    if (!container) return;
    const currentCount = container.querySelectorAll('.stop-input-row').length;
    if (currentCount >= MAX_STOPS) return;

    const idx = currentCount; // 0-based index for this stop
    stopItems[idx] = null;
    stopInputTimers[idx] = null;
    stopDwellTimes[idx] = 0;

    // Create row wrapper
    const row = document.createElement('div');
    row.className = 'stop-input-row';
    row.dataset.stopIdx = String(idx);

    // Input group
    const group = document.createElement('div');
    group.className = 'input-group';

    const label = document.createElement('label');
    label.textContent = `Via Stop ${idx + 1}:`;
    label.htmlFor = `stopPoint${idx}`;

    const input = document.createElement('input');
    input.type = 'text';
    input.id = `stopPoint${idx}`;
    input.className = 'stop-point-input';
    input.placeholder = 'Enter intermediate stop';
    input.dataset.idx = String(idx);

    group.appendChild(label);
    group.appendChild(input);

    // Dwell time group
    const dwellGroup = document.createElement('div');
    dwellGroup.className = 'input-group stop-dwell-group';

    const dwellLabel = document.createElement('label');
    const tNow = translations[currentLang] || translations.en;
    dwellLabel.textContent = tNow.stopDwellLabel || 'Stop time (min):';
    dwellLabel.htmlFor = `stopDwell${idx}`;

    const dwellSelect = document.createElement('select');
    dwellSelect.id = `stopDwell${idx}`;
    dwellSelect.className = 'stop-dwell-select';
    dwellSelect.dataset.idx = String(idx);
    dwellSelect.setAttribute('aria-label', `Stop time in minutes for via stop ${idx + 1}`);
    // Options: 0 to 120 minutes in 5-minute increments
    for (let min = 0; min <= 120; min += 5) {
        const opt = document.createElement('option');
        opt.value = String(min);
        opt.textContent = min === 0 ? '0 (no wait)' : String(min);
        dwellSelect.appendChild(opt);
    }
    dwellSelect.value = '0';
    dwellSelect.addEventListener('change', () => {
        const dIdx = parseInt(dwellSelect.dataset.idx, 10);
        stopDwellTimes[dIdx] = parseInt(dwellSelect.value, 10) || 0;
    });

    dwellGroup.appendChild(dwellLabel);
    dwellGroup.appendChild(dwellSelect);

    // Remove button
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'remove-stop-btn';
    removeBtn.setAttribute('aria-label', `Remove stop ${idx + 1}`);
    removeBtn.textContent = '✕';
    removeBtn.addEventListener('click', () => {
        removeStopRow(row, idx);
    });

    row.appendChild(group);
    row.appendChild(dwellGroup);
    row.appendChild(removeBtn);
    container.appendChild(row);

    // Create suggestions dropdown for this stop
    const suggestions = document.createElement('div');
    suggestions.id = `stopSuggestions${idx}`;
    suggestions.className = 'suggestions-dropdown';
    document.body.appendChild(suggestions);

    // Input → suggestions
    input.addEventListener('input', async () => {
        const stopIdx = parseInt(input.dataset.idx, 10);
        const q = input.value.trim();
        if (stopItems[stopIdx]) {
            const sel = getLabelFromItem(stopItems[stopIdx]).trim().toLowerCase();
            if (q.toLowerCase() !== sel) stopItems[stopIdx] = null;
        }
        if (q.length < 3) { clearStopSuggestions(stopIdx); return; }
        try {
            const locs = await getPossibleLocations(q);
            renderStopSuggestions(stopIdx, locs || [], input);
        } catch (err) {
            clearStopSuggestions(stopIdx);
        }
    });

    // Debounced exact-match lookup
    input.addEventListener('input', () => {
        const stopIdx = parseInt(input.dataset.idx, 10);
        if (stopInputTimers[stopIdx]) clearTimeout(stopInputTimers[stopIdx]);
        const q = input.value.trim();
        if (!q) return;
        showInputLoading(input);
        stopInputTimers[stopIdx] = setTimeout(async () => {
            stopInputTimers[stopIdx] = null;
            hideInputLoading(input);
            if (!q) return;
            if (stopItems[stopIdx] && getLabelFromItem(stopItems[stopIdx]).trim().toLowerCase() === q.toLowerCase()) return;
            try {
                const locs = await getPossibleLocations(q);
                if (locs && locs.length) {
                    const match = locs.find(it => getLabelFromItem(it).trim().toLowerCase() === q.toLowerCase());
                    if (match) { stopItems[stopIdx] = match; addViaMarker(match, stopIdx); clearStopSuggestions(stopIdx); }
                }
            } catch (err) { /* silent */ }
        }, 2000);
    });

    input.addEventListener('keydown', (e) => {
        const stopIdx = parseInt(input.dataset.idx, 10);
        const sug = document.getElementById(`stopSuggestions${stopIdx}`);
        if (sug) handleInputKeydown(e, input, sug);
    });

    // Set this stop as the active map input when focused, so clicking the map fills it
    input.addEventListener('focus', () => {
        setActiveMapInput({ type: 'stop', idx: parseInt(input.dataset.idx, 10) });
    });

    input.focus();
    refreshAddStopBtn();
}

function clearStopSuggestions(idx) {
    const sug = document.getElementById(`stopSuggestions${idx}`);
    if (!sug) return;
    sug.innerHTML = '';
    sug.style.display = 'none';
}

function renderStopSuggestions(idx, items, inputEl) {
    clearStopSuggestions(idx);
    if (!items || items.length === 0) return;
    const sug = document.getElementById(`stopSuggestions${idx}`);
    if (!sug) return;

    const list = document.createElement('ul');
    list.setAttribute('role', 'listbox');
    list.className = 'suggestions-list';
    items.slice(0, 5).forEach((it, i) => {
        const label = getLabelFromItem(it);
        const li = document.createElement('li');
        li.className = 'suggestion-item';
        li.setAttribute('role', 'option');
        li.setAttribute('data-index', String(i));
        li.tabIndex = 0;
        li.textContent = label;
        li.addEventListener('click', () => {
            stopItems[idx] = it;
            addViaMarker(it, idx);
            if (inputEl) inputEl.value = label;
            clearStopSuggestions(idx);
            if (inputEl) inputEl.focus();
            if (stopInputTimers[idx]) { clearTimeout(stopInputTimers[idx]); stopInputTimers[idx] = null; hideInputLoading(inputEl); }
        });
        li.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); li.click(); } });
        list.appendChild(li);
    });
    sug._items = items;
    sug.appendChild(list);

    // Position below the input
    const rect = inputEl.getBoundingClientRect();
    const scrollY = window.scrollY || window.pageYOffset;
    const scrollX = window.scrollX || window.pageXOffset;
    sug.style.width = rect.width + 'px';
    sug.style.left = (rect.left + scrollX) + 'px';
    sug.style.top = (rect.bottom + scrollY + 6) + 'px';
    sug.style.display = 'block';
    sug.dataset.active = '-1';
}

/**
 * Remove a stop row and clean up state.
 */
function removeStopRow(row, removedIdx) {
    const container = document.getElementById('stopsContainer');
    if (!container) return;

    // Remove associated suggestions dropdown
    const sug = document.getElementById(`stopSuggestions${removedIdx}`);
    if (sug) sug.remove();

    // Remove the via marker for the removed stop
    removeViaMarker(removedIdx);

    row.remove();

    // Re-index remaining rows
    const rows = container.querySelectorAll('.stop-input-row');
    const newStopItems = [];
    const newDwellTimes = [];
    const newViaMarkers = [];
    rows.forEach((r, newIdx) => {
        const oldIdx = parseInt(r.dataset.stopIdx, 10);
        r.dataset.stopIdx = String(newIdx);

        const inp = r.querySelector('.stop-point-input');
        if (inp) {
            inp.dataset.idx = String(newIdx);
            inp.id = `stopPoint${newIdx}`;
        }
        // Re-index dwell select
        const dwellSel = r.querySelector('.stop-dwell-select');
        if (dwellSel) {
            dwellSel.dataset.idx = String(newIdx);
            dwellSel.id = `stopDwell${newIdx}`;
        }
        // Re-bind dwell change handler with new index
        const dwellSelNew = dwellSel ? dwellSel.cloneNode(true) : null;
        if (dwellSelNew) {
            dwellSelNew.addEventListener('change', () => {
                stopDwellTimes[parseInt(dwellSelNew.dataset.idx, 10)] = parseInt(dwellSelNew.value, 10) || 0;
            });
            if (dwellSel) dwellSel.replaceWith(dwellSelNew);
        }

        // Update only the first label (via stop label, not dwell label)
        const labels = r.querySelectorAll('label');
        if (labels[0]) { labels[0].textContent = `Via Stop ${newIdx + 1}:`; labels[0].htmlFor = `stopPoint${newIdx}`; }
        if (labels[1]) { labels[1].htmlFor = `stopDwell${newIdx}`; }

        const removeBtn = r.querySelector('.remove-stop-btn');
        if (removeBtn) {
            removeBtn.setAttribute('aria-label', `Remove stop ${newIdx + 1}`);
            // Re-bind click with new idx
            const newRemoveBtn = removeBtn.cloneNode(true);
            newRemoveBtn.addEventListener('click', () => removeStopRow(r, newIdx));
            removeBtn.replaceWith(newRemoveBtn);
        }

        // Move old suggestions dropdown ID
        const oldSug = document.getElementById(`stopSuggestions${oldIdx}`);
        if (oldSug && oldIdx !== newIdx) { oldSug.id = `stopSuggestions${newIdx}`; }

        newStopItems[newIdx] = stopItems[oldIdx] || null;
        newDwellTimes[newIdx] = stopDwellTimes[oldIdx] || 0;
        newViaMarkers[newIdx] = viaMarkers[oldIdx] || null;
    });

    stopItems.length = 0;
    stopInputTimers.length = 0;
    stopDwellTimes.length = 0;
    newStopItems.forEach((it, i) => { stopItems[i] = it; stopInputTimers[i] = null; stopDwellTimes[i] = newDwellTimes[i] || 0; });
    viaMarkers.length = 0;
    newViaMarkers.forEach((m, i) => { viaMarkers[i] = m || null; });

    // If the removed stop was the active map input, clear it
    if (activeMapInput && activeMapInput.type === 'stop') {
        setActiveMapInput(null);
    }

    refreshAddStopBtn();
}

// Wire up the "Add Stop" button
document.addEventListener('DOMContentLoaded', () => {
    const addStopBtn = document.getElementById('addStopBtn');
    if (addStopBtn) {
        addStopBtn.addEventListener('click', () => addStopRow());
    }

    // Close stop suggestions on outside click
    document.addEventListener('click', (e) => {
        const inputs = document.querySelectorAll('.stop-point-input');
        inputs.forEach((inp) => {
            const idx = parseInt(inp.dataset.idx, 10);
            const sug = document.getElementById(`stopSuggestions${idx}`);
            if (sug && !sug.contains(e.target) && e.target !== inp) clearStopSuggestions(idx);
        });
    });
});

// Distinct colours used to distinguish consecutive bus legs on the map.
const LEG_COLOURS = ['#E74C3C', '#8E44AD', '#2980B9', '#27AE60', '#F39C12', '#16A085', '#D35400', '#2C3E50'];

/**
 * Scroll to and briefly flash the journey-leg panel entry that corresponds
 * to the given leg object.  Called when the user clicks a polyline on the map.
 */
function highlightLegInPanel(leg) {
    document.querySelectorAll('.journey-leg').forEach(el => {
        if (el._leg !== leg) return;
        // Ensure the parent journey card is expanded so the leg is visible
        const legsEl = el.closest('.journey-legs');
        if (legsEl && legsEl.style.display === 'none') {
            legsEl.style.display = 'block';
            const card = legsEl.closest('.journey-card');
            if (card) {
                const toggleIcon = card.querySelector('.journey-toggle-icon');
                if (toggleIcon) toggleIcon.textContent = '▼';
            }
        }
        el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        // Restart the CSS animation by removing and re-adding the class
        el.classList.remove('journey-leg--highlighted');
        void el.offsetWidth; // force reflow
        el.classList.add('journey-leg--highlighted');
    });
}

async function drawJourneyOnMap(journey) {
    if (!map) return;
    clearRouteLayers();
    routeLayerGroup = L.layerGroup().addTo(map);
    legToPolylineMap = new Map();

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
        // Fallback: use OTP-provided coordinates embedded in the leg
        const isOrigin = key === 'origin_stop_id';
        const latKey = isOrigin ? 'origin_lat' : 'destination_lat';
        const lonKey = isOrigin ? 'origin_lon' : 'destination_lon';
        if (leg[latKey] != null && leg[lonKey] != null) {
            const coords = [parseFloat(leg[latKey]), parseFloat(leg[lonKey])];
            if (!Number.isNaN(coords[0]) && !Number.isNaN(coords[1])) {
                coordCache[sid] = coords;
                return coords;
            }
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
            let walkPline = null;
            if (leg.waypoints && leg.waypoints.length > 1) {
                // Use the detailed walking geometry supplied by OTP.
                walkPline = L.polyline(leg.waypoints, walkStyle).addTo(routeLayerGroup);
            } else {
                // Fall back to OpenRouteService or a straight line between the
                // previous bus leg's last stop and the next bus leg's first stop.
                // If lastPoint is unknown (e.g. DB is empty), derive from the
                // walk leg's own OTP-provided coordinates.
                let walkFrom = lastPoint;
                if (!walkFrom && leg.from_lat != null && leg.from_lon != null) {
                    walkFrom = [parseFloat(leg.from_lat), parseFloat(leg.from_lon)];
                }
                let nextBusCoords = null;
                for (let j = i + 1; j < legs.length; j++) {
                    if ((legs[j].mode || 'bus') === 'walk') continue;
                    nextBusCoords = await coordsForLegEndpoint(legs[j], 'origin_stop_id');
                    if (nextBusCoords) break;
                }
                // Also try the walk leg's own destination coordinates
                if (!nextBusCoords && leg.to_lat != null && leg.to_lon != null) {
                    nextBusCoords = [parseFloat(leg.to_lat), parseFloat(leg.to_lon)];
                }
                if (walkFrom && nextBusCoords) {
                    try {
                        console.log(`Routing walking leg from ${walkFrom} to ${nextBusCoords}`);
                        const routed = await routeAlongRoad(walkFrom, nextBusCoords, 'walking');
                        if (routed && routed.length) {
                            walkPline = L.polyline(routed, walkStyle).addTo(routeLayerGroup);
                        } else {
                            walkPline = L.polyline([walkFrom, nextBusCoords], walkStyle).addTo(routeLayerGroup);
                        }
                    } catch (err) {
                        walkPline = L.polyline([walkFrom, nextBusCoords], walkStyle).addTo(routeLayerGroup);
                    }
                }
            }
            if (walkPline) {
                legToPolylineMap.set(leg, walkPline);
                walkPline.on('click', () => highlightLegInPanel(leg));
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
            const popupA = `<div><strong>${leg.origin_stop_name || leg.from_stop || leg.origin_stop_id || ''}</strong>${leg.departure_time ? `<div class="popup-depart-time">Dep: ${leg.departure_time}</div>` : ''}</div>`;
            L.circleMarker(a, { radius: 6, color: legColor, fillColor: '#fff', weight: 2 }).addTo(routeLayerGroup).bindPopup(popupA);
        }
        if (b) {
            const popupB = `<div><strong>${leg.destination_stop_name || leg.to_stop || leg.destination_stop_id || ''}</strong>${leg.arrival_time ? `<div class="popup-arrive-time">Arr: ${leg.arrival_time}</div>` : ''}</div>`;
            L.circleMarker(b, { radius: 6, color: legColor, fillColor: '#fff', weight: 2 }).addTo(routeLayerGroup).bindPopup(popupB);
        }
        // Draw route for this (non-walking) leg.
        // Prefer DB-backed geometry returned directly by /journey/plan,
        // then OTP geometry, then a direct /routes fallback, then straight line.
        if (a && b) {
            let waypoints = null;
            if (leg.waypoints && leg.waypoints.length > 1) {
                waypoints = leg.waypoints.map(pt => [pt.lat, pt.lon]);
            } else if (leg.otp_waypoints && leg.otp_waypoints.length > 1) {
                waypoints = leg.otp_waypoints;
            } else {
                waypoints = await fetchRouteWaypoints(
                    leg.route_id, leg.direction,
                    leg.origin_stop_id, leg.destination_stop_id
                );
            }
            if (waypoints && waypoints.length > 1) {
                const pline = L.polyline(waypoints, { color: legColor, weight: 4, opacity: 0.85 }).addTo(routeLayerGroup);
                legToPolylineMap.set(leg, pline);
                pline.on('click', () => highlightLegInPanel(leg));
            } else {
                const pline = L.polyline([a, b], { color: legColor, weight: 4, opacity: 0.85 }).addTo(routeLayerGroup);
                legToPolylineMap.set(leg, pline);
                pline.on('click', () => highlightLegInPanel(leg));
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

/**
 * Open a modal showing detailed information about a journey leg.
 * Displays: operator, route number, direction, and all stops on the route.
 */
function openLegDetailsModal(leg, modeLabel, operatorName, routeName) {
    const t = translations[currentLang] || translations.en;

    // Create overlay backdrop
    const backdrop = document.createElement('div');
    backdrop.className = 'leg-modal-backdrop';

    // Create modal
    const modal = document.createElement('div');
    modal.className = 'leg-modal';

    // Close button
    const closeBtn = document.createElement('button');
    closeBtn.className = 'leg-modal-close';
    closeBtn.setAttribute('aria-label', 'Close');
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', () => {
        backdrop.remove();
    });

    const canLoadIntermediateStops = !!(
        leg.route_id && leg.origin_stop_id && leg.destination_stop_id
    );

    // Modal header
    const header = document.createElement('div');
    header.className = 'leg-modal-header';

    const title = document.createElement('h3');
    title.textContent = routeName || 'Route Details';
    header.appendChild(title);
    header.appendChild(closeBtn);

    // Modal body
    const body = document.createElement('div');
    body.className = 'leg-modal-body';

    // Operator
    if (operatorName) {
        const operatorSection = document.createElement('div');
        operatorSection.className = 'leg-modal-section';
        const operatorLabel = document.createElement('strong');
        operatorLabel.textContent = 'Operator:';
        const operatorValue = document.createElement('div');
        operatorValue.textContent = operatorName;
        operatorSection.appendChild(operatorLabel);
        operatorSection.appendChild(operatorValue);
        body.appendChild(operatorSection);
    }

    // Direction
    if (leg.direction) {
        const directionSection = document.createElement('div');
        directionSection.className = 'leg-modal-section';
        const directionLabel = document.createElement('strong');
        directionLabel.textContent = 'Direction:';
        const directionValue = document.createElement('div');
        directionValue.textContent = leg.direction.charAt(0).toUpperCase() + leg.direction.slice(1);
        directionSection.appendChild(directionLabel);
        directionSection.appendChild(directionValue);
        body.appendChild(directionSection);
    }

    // From and To
    const fromToSection = document.createElement('div');
    fromToSection.className = 'leg-modal-section';
    const fromToLabel = document.createElement('strong');
    fromToLabel.textContent = 'Journey:';
    const fromToValue = document.createElement('div');
    fromToValue.className = 'leg-modal-journey-fromto';
    const startStopName = document.createElement('div');
    startStopName.className = 'leg-modal-stop-name';
    const startStopStrong = document.createElement('strong');
    startStopStrong.textContent = leg.origin_stop_name || leg.from_stop || leg.origin_stop_id || 'Start';
    startStopName.appendChild(startStopStrong);

    const journeyArrow = document.createElement('div');
    journeyArrow.className = 'leg-modal-journey-arrow';
    journeyArrow.textContent = '↓';

    fromToValue.appendChild(startStopName);
    fromToValue.appendChild(journeyArrow);

    const intermediateStopsWrap = document.createElement('div');
    intermediateStopsWrap.className = 'leg-modal-intermediate-wrap';
    fromToValue.appendChild(intermediateStopsWrap);

    const loadStatus = document.createElement('div');
    loadStatus.className = 'leg-modal-load-status';

    const normalizeStopId = (value) => String(value || '').trim().toLowerCase();
    const originIdNorm = normalizeStopId(leg.origin_stop_id);
    const destinationIdNorm = normalizeStopId(leg.destination_stop_id);

    if (canLoadIntermediateStops) {
        const inlineLoadWrap = document.createElement('div');
        inlineLoadWrap.className = 'leg-modal-inline-load-wrap';

        const loadToEndArrow = document.createElement('div');
        loadToEndArrow.className = 'leg-modal-journey-arrow';
        loadToEndArrow.textContent = '↓';

        const loadBtn = document.createElement('button');
        loadBtn.className = 'leg-modal-load-stops-btn';
        loadBtn.type = 'button';
        loadBtn.textContent = 'Load Stops';

        const renderStops = (stops) => {
            intermediateStopsWrap.innerHTML = '';

            if (!Array.isArray(stops) || stops.length === 0) {
                return;
            }

            const intermediateStops = stops.filter((s) => {
                const sid = normalizeStopId(s && s.stop_id);
                if (!sid) return true;
                return sid !== originIdNorm && sid !== destinationIdNorm;
            });

            if (intermediateStops.length === 0) {
                return;
            }

            intermediateStops.forEach((s, idx) => {
                const stopEl = document.createElement('div');
                stopEl.className = 'leg-modal-stop-name';

                const stopStrong = document.createElement('strong');
                const name = s.stop_name || s.stop_id || `Stop ${idx + 1}`;
                const arr = s.arrival_time || '';
                stopStrong.textContent = arr ? `${name} (${arr})` : name;

                stopEl.appendChild(stopStrong);
                intermediateStopsWrap.appendChild(stopEl);

                const arrowEl = document.createElement('div');
                arrowEl.className = 'leg-modal-journey-arrow';
                arrowEl.textContent = '↓';
                intermediateStopsWrap.appendChild(arrowEl);
            });
        };

        loadBtn.addEventListener('click', async () => {
            loadBtn.disabled = true;
            loadBtn.textContent = 'Loading…';
            loadStatus.textContent = '';

            const payload = await fetchRouteStopsBetween(
                leg.route_id,
                leg.direction,
                leg.origin_stop_id,
                leg.destination_stop_id,
                leg.departure_time,
            );

            if (!payload || !Array.isArray(payload.stops)) {
                loadStatus.textContent = 'Could not load route stops for this leg.';
                loadBtn.disabled = false;
                loadBtn.textContent = 'Load Stops';
                return;
            }

            loadStatus.textContent = '';
            renderStops(payload.stops);
            inlineLoadWrap.remove();
            loadToEndArrow.remove();
        });

        inlineLoadWrap.appendChild(loadBtn);
        fromToValue.appendChild(inlineLoadWrap);
        fromToValue.appendChild(loadToEndArrow);
    }

    const endStopName = document.createElement('div');
    endStopName.className = 'leg-modal-stop-name';
    const endStopStrong = document.createElement('strong');
    endStopStrong.textContent = leg.destination_stop_name || leg.to_stop || leg.destination_stop_id || 'End';
    endStopName.appendChild(endStopStrong);
    fromToValue.appendChild(endStopName);

    fromToSection.appendChild(fromToLabel);
    fromToSection.appendChild(fromToValue);
    fromToSection.appendChild(loadStatus);
    body.appendChild(fromToSection);

    // Times
    if (leg.departure_time || leg.arrival_time) {
        const timesSection = document.createElement('div');
        timesSection.className = 'leg-modal-section';
        const timesLabel = document.createElement('strong');
        timesLabel.textContent = 'Times:';
        const timesValue = document.createElement('div');
        let timesHTML = '';
        if (leg.departure_time) {
            timesHTML += `<div>Depart: <span style="color: var(--journey-departs-color); font-weight: 600;">${leg.departure_time}</span></div>`;
        }
        if (leg.arrival_time) {
            timesHTML += `<div>Arrive: <span style="color: var(--journey-arrives-color); font-weight: 600;">${leg.arrival_time}</span></div>`;
        }
        timesValue.innerHTML = timesHTML;
        timesSection.appendChild(timesLabel);
        timesSection.appendChild(timesValue);
        body.appendChild(timesSection);
    }

    // Live delay information section
    if (leg.delay_source) {
        const delaySection = document.createElement('div');
        delaySection.className = 'leg-modal-section';
        const delayLabel = document.createElement('strong');
        delayLabel.textContent = 'Live Status:';
        const delayValue = document.createElement('div');
        const isLiveSource = leg.delay_source === 'live_position' || leg.delay_source === 'darwin' || leg.delay_source === 'route_estimate';
        const isDarwin = leg.delay_source === 'darwin';
        const isRouteEstimate = leg.delay_source === 'route_estimate';

        if (leg.is_cancelled) {
            let cancelHTML = `<span class="journey-leg-delay-badge">❌ Cancelled</span>`;
            if (leg.cancel_reason) cancelHTML += `<div class="journey-leg-delay-reason">${leg.cancel_reason}</div>`;
            delayValue.innerHTML = cancelHTML;
        } else if (leg.estimated_delay_mins != null && leg.estimated_delay_mins > 1 && isLiveSource) {
            const qualifier = isRouteEstimate ? '~' : '~';
            let delayHTML = `<span class="journey-leg-delay-badge">⚠️ Estimated ${qualifier}${leg.estimated_delay_mins} min delay</span>`;
            if (isRouteEstimate) delayHTML += ` <small>(based on nearby vehicle)</small>`;
            if (leg.estimated_departure) delayHTML += `<div>Expected departure: <strong>${leg.estimated_departure}</strong></div>`;
            if (leg.delay_reason) delayHTML += `<div class="journey-leg-delay-reason">${leg.delay_reason}</div>`;
            delayValue.innerHTML = delayHTML;
        } else if (isLiveSource && leg.estimated_delay_mins != null) {
            const src = isDarwin ? 'National Rail live feed' : isRouteEstimate ? 'nearby vehicle on route' : 'live vehicle position';
            delayValue.innerHTML = `<span class="journey-leg-ontime-badge">✅ On time</span> <small>(${src})</small>`;
        } else {
            delayValue.innerHTML = `<span class="journey-leg-schedule-badge">📅 Scheduled times (no live data available)</span>`;
        }

        delaySection.appendChild(delayLabel);
        delaySection.appendChild(delayValue);
        body.appendChild(delaySection);

        // Rail-specific extra detail
        if (isDarwin) {
            const railDetail = document.createElement('div');
            railDetail.className = 'leg-modal-section';
            const railLabel = document.createElement('strong');
            railLabel.textContent = 'Rail Service Detail:';
            let detailHTML = '';
            if (leg.platform) detailHTML += `<div>🚏 Platform <strong>${leg.platform}</strong></div>`;
            if (leg.operator) detailHTML += `<div>Operator: ${leg.operator}</div>`;
            if (leg.service_id) detailHTML += `<div>Service ID: <code>${leg.service_id}</code></div>`;
            if (leg.destination_delay_mins != null) {
                const destStatus = leg.destination_delay_mins === 0
                    ? '✅ On time at destination'
                    : `⚠️ ~${leg.destination_delay_mins} min delay at destination`;
                detailHTML += `<div>${destStatus}</div>`;
                if (leg.destination_estimated_time) detailHTML += `<div>Estimated arrival: <strong>${leg.destination_estimated_time}</strong></div>`;
            }
            if (detailHTML) {
                const railValue = document.createElement('div');
                railValue.innerHTML = detailHTML;
                railDetail.appendChild(railLabel);
                railDetail.appendChild(railValue);
                body.appendChild(railDetail);
            }
        }
    }

    // Additional info message
    const infoMsg = document.createElement('div');
    infoMsg.className = 'leg-modal-info';
    infoMsg.textContent = 'Tip: click “Load Stops” to view all calls and times for this leg.';
    body.appendChild(infoMsg);

    modal.appendChild(header);
    modal.appendChild(body);
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);
}

/**
 * Calculate journey duration in minutes from start and end time strings (HH:MM).
 * A negative raw difference is treated as an overnight journey crossing midnight.
 * Returns null for times that appear invalid (e.g., > 24 h difference after correction).
 */
function calcJourneyDurationMins(startTime, endTime) {
    if (!startTime || !endTime) return null;
    const [sh, sm] = startTime.split(':').map(Number);
    const [eh, em] = endTime.split(':').map(Number);
    if (isNaN(sh) || isNaN(sm) || isNaN(eh) || isNaN(em)) return null;
    let total = (eh * 60 + em) - (sh * 60 + sm);
    // A negative value most likely means the journey crosses midnight (e.g. departs
    // 23:50 and arrives 00:30 the next day). Add 24 h to correct for this.
    if (total < 0) total += 24 * 60;
    // Guard against implausibly long results that indicate bad data (> 24 h).
    if (total > 24 * 60) return null;
    return total;
}

/**
 * Format a duration in minutes as a human-readable string (e.g. "1h 25m").
 */
function formatJourneyDuration(mins) {
    if (mins === null || mins < 0) return '';
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    if (h > 0 && m > 0) return `${h}h ${m}m`;
    if (h > 0) return `${h}h`;
    return `${m}m`;
}

function renderJourneyList(journeys, departureDate) {
    const t = translations[currentLang] || translations.en;

    const operatorMap = {
        SCCU: 'Stagecoach',
        SCMY: 'Stagecoach',
        ARCT: 'Archway Travel',
        BLAC: 'Blackpool Transport',
        KLCO: 'Kirkby Lonsdale Coach Hire',
        NUTT: 'Transporta North West',
        TP: 'TransPennine Express',
        VT: 'Avanti West Coast',
        GR: 'London North Eastern Railway',
        GW: 'Great Western Railway',
        EM: 'East Midlands Railway',
        AW: 'Transport for Wales',
        NT: 'Northern',
        SR: 'ScotRail',
        SW: 'South Western Railway',
        SE: 'Southeastern',
        SN: 'Southern',
        XR: 'Elizabeth line',
    };

    function normalizeOperatorName(raw) {
        if (!raw) return '';
        const trimmed = String(raw).trim();
        if (!trimmed) return '';
        return operatorMap[trimmed] || trimmed;
    }

    // Build journey mode sequence (e.g. "Walk → Bus → Train → Walk")
    function buildModeSequence(legs) {
        if (!legs || legs.length === 0) return '';
        const modes = [];
        for (const leg of legs) {
            const mode = (leg.mode || 'bus').toLowerCase();
            if (mode === 'walk') {
                if (!modes.length || modes[modes.length - 1] !== '🚶') modes.push('🚶');
            } else if (mode === 'rail' || mode === 'train') {
                modes.push('🚆');
            } else if (mode === 'tram') {
                modes.push('🚋');
            } else {
                modes.push('🚌');
            }
        }
        return modes.join(' → ');
    }

    // Format departure date as a human-readable label (e.g. "Tuesday 11 Mar")
    let departureDayLabel = '';
    if (departureDate) {
        // departureDate is "YYYY-MM-DD"; parse as local date to avoid UTC offset issues
        const [year, month, day] = departureDate.split('-').map(Number);
        const dateObj = new Date(year, month - 1, day);
        departureDayLabel = dateObj.toLocaleDateString(
            currentLang === 'zh' ? 'zh-CN' : 'en-GB',
            { weekday: 'long', day: 'numeric', month: 'short' }
        );
    }
    if (!journeys || journeys.length === 0) {
        routeContent.innerHTML = `<p class="journey-no-results">${t.noJourneys}</p>`;
        clearRouteLayers();
        return;
    }

    // Deduplicate journeys: keep distinct walking/transit patterns.
    // Previous logic only keyed by transit legs and could hide itineraries that
    // differ mainly by walking amount/segments.
    const dedupedJourneys = [];
    const seenKeys = new Set();
    for (const j of journeys) {
        const legs = j.legs || [];
        const key = legs.map((l) => {
            const mode = (l.mode || 'bus').toLowerCase();
            if (mode === 'walk') {
                const from = (l.from_stop || l.from || '').trim();
                const to = (l.to_stop || l.to || '').trim();
                const dist = Number(l.distance_km || 0).toFixed(3);
                return `walk:${from}->${to}:${l.departure_time || ''}:${l.arrival_time || ''}:${dist}`;
            }
            return [
                mode,
                l.route_id || '',
                l.origin_stop_id || l.origin_stop_name || l.from_stop || '',
                l.destination_stop_id || l.destination_stop_name || l.to_stop || '',
                l.departure_time || '',
                l.arrival_time || '',
            ].join(':');
        }).join('|');

        if (!seenKeys.has(key)) {
            seenKeys.add(key);
            dedupedJourneys.push(j);
        }
    }

    // Build a compact list with collapsible journey cards
    const container = document.createElement('div');
    container.className = 'journey-list';

    // Journey results count header
    const resultsHeader = document.createElement('div');
    resultsHeader.className = 'journey-results-header';
    const count = dedupedJourneys.length;
    resultsHeader.textContent = `${count} ${count === 1 ? 'journey' : 'journeys'} found`;
    container.appendChild(resultsHeader);

    // Track which journey is currently expanded
    let expandedJourneyIdx = 0;

    dedupedJourneys.forEach((j, idx) => {
        const card = document.createElement('div');
        card.className = 'journey-card';
        card.dataset.journeyIdx = idx;

        // Get all non-walk legs for finding first and last transit stops
        const busLegs = j.legs && j.legs.filter(l => (l.mode || 'bus') !== 'walk') || [];
        const firstBusLeg = busLegs.length > 0 ? busLegs[0] : null;
        const lastBusLeg = busLegs.length > 0 ? busLegs[busLegs.length - 1] : null;

        // Use first and last legs (including walks) for overall journey departure/arrival times
        const allLegs = j.legs || [];
        const firstLeg = allLegs.length > 0 ? allLegs[0] : null;
        const lastLeg = allLegs.length > 0 ? allLegs[allLegs.length - 1] : null;

        // Build mode sequence (e.g. "🚶 → 🚌 → 🚆 → 🚶")
        const modeSequence = buildModeSequence(j.legs || []);

        // Header: clickable collapse/expand toggle
        const header = document.createElement('div');
        header.className = 'journey-card-header';
        header.style.cursor = 'pointer';

        // Left side: Mode sequence + origin/destination
        const summarySection = document.createElement('div');
        summarySection.className = 'journey-summary-section';

        const modeBar = document.createElement('div');
        modeBar.className = 'journey-mode-sequence';
        modeBar.textContent = modeSequence;
        summarySection.appendChild(modeBar);

        const summary = document.createElement('div');
        summary.className = 'journey-summary';

        const originEl = document.createElement('div');
        originEl.className = 'journey-stop-name';
        originEl.textContent = (firstBusLeg && (firstBusLeg.origin_stop_name || firstBusLeg.from_stop || firstBusLeg.origin_stop_id)) || '';

        const arrowEl = document.createElement('div');
        arrowEl.className = 'journey-stop-arrow';
        arrowEl.textContent = '→';

        const destEl = document.createElement('div');
        destEl.className = 'journey-stop-name';
        destEl.textContent = (lastBusLeg && (lastBusLeg.destination_stop_name || lastBusLeg.to_stop || lastBusLeg.destination_stop_id)) || '';

        summary.appendChild(originEl);
        summary.appendChild(arrowEl);
        summary.appendChild(destEl);
        summarySection.appendChild(summary);

        // Duration badge
        const durationMins = calcJourneyDurationMins(
            firstLeg && firstLeg.departure_time,
            lastLeg && lastLeg.arrival_time
        );
        const durationStr = formatJourneyDuration(durationMins);
        if (durationStr) {
            const durationBadge = document.createElement('div');
            durationBadge.className = 'journey-duration-badge';
            durationBadge.textContent = `⏱ ${durationStr}`;
            summarySection.appendChild(durationBadge);
        }

        // Already-departed warning — shown when the first leg departs before the current time
        if (firstLeg && firstLeg.departure_time && departureDate) {
            const [dY, dM, dD] = departureDate.split('-').map(Number);
            const [tH, tM] = firstLeg.departure_time.split(':').map(Number);
            if (!isNaN(dY) && !isNaN(tH)) {
                const depDate = new Date(dY, dM - 1, dD, tH, tM || 0);
                if (depDate < new Date()) {
                    const deptBadge = document.createElement('div');
                    deptBadge.className = 'journey-departed-warning';
                    deptBadge.textContent = t.alreadyDepartedWarning || '⚠️ This journey has already departed';
                    summarySection.appendChild(deptBadge);
                }
            }
        }

        // Late-arrival warning badge (for arrive-by searches where target wasn't met)
        if (j.arrives_late && j.late_by_mins) {
            const lateBadge = document.createElement('div');
            lateBadge.className = 'journey-late-warning';
            const lateTemplate = (t.arrivesLateWarning || '⚠️ Arrives {mins} min after target time');
            lateBadge.textContent = lateTemplate.replace('{mins}', j.late_by_mins);
            summarySection.appendChild(lateBadge);
        }

        // Right side: Times
        const times = document.createElement('div');
        times.className = 'journey-times';

        if (departureDayLabel) {
            const dayLabel = document.createElement('div');
            dayLabel.className = 'journey-day-label';
            dayLabel.textContent = departureDayLabel;
            times.appendChild(dayLabel);
        }

        // Departure block
        const depBlock = document.createElement('div');
        depBlock.className = 'journey-departs-block';
        const depLabel = document.createElement('span');
        depLabel.className = 'journey-departs-label';
        depLabel.textContent = t.departs;
        const depTime = document.createElement('div');
        depTime.className = 'journey-departs-time';
        depTime.textContent = firstLeg && firstLeg.departure_time || '';
        depBlock.appendChild(depLabel);
        depBlock.appendChild(depTime);

        // Arrival block
        const arrBlock = document.createElement('div');
        arrBlock.className = 'journey-arrives-block';
        const arrLabel = document.createElement('span');
        arrLabel.className = 'journey-arrives-label';
        arrLabel.textContent = t.arrives;
        const arrTime = document.createElement('div');
        arrTime.className = 'journey-arrives-time';
        arrTime.textContent = lastLeg && lastLeg.arrival_time || '';
        arrBlock.appendChild(arrLabel);
        arrBlock.appendChild(arrTime);

        times.appendChild(depBlock);
        times.appendChild(arrBlock);

        // Expand/collapse toggle indicator
        const toggleIcon = document.createElement('div');
        toggleIcon.className = 'journey-toggle-icon';
        toggleIcon.textContent = idx === 0 ? '▼' : '▶';

        header.appendChild(summarySection);
        header.appendChild(times);
        header.appendChild(toggleIcon);

        card.appendChild(header);

        // Per-leg details (hidden when collapsed)
        const legsEl = document.createElement('div');
        legsEl.className = 'journey-legs';
        legsEl.style.display = idx === 0 ? 'block' : 'none'; // Show first journey by default

        const ul = document.createElement('ul');
        ul.className = 'journey-legs-list';

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

        // Remove short trailing walk legs (< 50 m) that can confuse users
        while (dedupedLegs.length > 1) {
            const last = dedupedLegs[dedupedLegs.length - 1];
            if ((last.mode || 'bus') === 'walk' && typeof last.distance_km === 'number' && last.distance_km < 0.05) {
                dedupedLegs.pop();
            } else {
                break;
            }
        }

        const finalTransitDest = lastBusLeg && (lastBusLeg.destination_stop_name || lastBusLeg.to_stop || lastBusLeg.destination_stop_id) || '';

        // Track the transit-leg colour index so the panel colours match the map
        let legColourIndex = 0;

        (dedupedLegs || []).forEach((leg) => {
            const li = document.createElement('li');
            li.className = 'journey-leg';
            if ((leg.mode || 'bus') === 'walk') {
                li.classList.add('journey-leg--walk');

                const walkLabel = document.createElement('span');
                walkLabel.className = 'journey-leg-walk-label';
                walkLabel.textContent = `🚶 ${t.walk}`;

                const walkDetail = document.createElement('span');
                walkDetail.className = 'journey-leg-walk-detail';
                walkDetail.textContent = `${leg.from_stop || leg.from || ''} → ${leg.to_stop || leg.to || leg.to_stop || ''}`;

                const walkContainer = document.createElement('div');
                walkContainer.appendChild(walkLabel);
                walkContainer.appendChild(walkDetail);

                if (leg.distance_km) {
                    const distEl = document.createElement('span');
                    distEl.className = 'journey-leg-walk-distance';
                    distEl.textContent = ` (${leg.distance_km} km)`;
                    walkDetail.appendChild(distEl);
                }

                li.appendChild(walkContainer);

                if (leg.departure_time || leg.arrival_time) {
                    const walkTimesDiv = document.createElement('div');
                    walkTimesDiv.className = 'journey-leg-times';
                    if (leg.departure_time) {
                        const depSpan = document.createElement('span');
                        depSpan.className = 'journey-leg-depart';
                        depSpan.textContent = leg.departure_time;
                        walkTimesDiv.appendChild(depSpan);
                    }
                    if (leg.departure_time && leg.arrival_time) {
                        const sepSpan = document.createElement('span');
                        sepSpan.className = 'journey-leg-sep';
                        sepSpan.textContent = ' → ';
                        walkTimesDiv.appendChild(sepSpan);
                    }
                    if (leg.arrival_time) {
                        const arrSpan = document.createElement('span');
                        arrSpan.className = 'journey-leg-arrive';
                        arrSpan.textContent = leg.arrival_time;
                        walkTimesDiv.appendChild(arrSpan);
                    }
                    li.appendChild(walkTimesDiv);
                }
            } else {
                // Assign the same colour used on the map for this transit leg
                const legColor = LEG_COLOURS[legColourIndex % LEG_COLOURS.length];
                legColourIndex++;
                // Apply the colour to the left border (matching the map polyline)
                li.style.borderLeftColor = legColor;

                const mode = (leg.mode || 'bus').toLowerCase();
                const isRail = mode === 'rail' || mode === 'train';
                const isTram = mode === 'tram';
                const modeIcon = isRail ? '🚆' : (isTram ? '🚋' : '🚌');
                const modeLabel = isRail ? t.train : (isTram ? t.tram : t.bus);

                const legDestLabel = leg.destination_stop_name || leg.to_stop || leg.destination_stop_id || '';
                const serviceDest = isRail
                    ? (leg.rail_service_destination || legDestLabel || finalTransitDest)
                    : legDestLabel;
                const route = isRail
                    ? `${t.serviceTo} ${serviceDest}`.trim()
                    : (leg.route_name ? `${leg.route_name}` : (leg.route_id || 'Route'));

                const operatorName = normalizeOperatorName(leg.operator || '');

                // Compact leg header with info button
                const legHeader = document.createElement('div');
                legHeader.className = 'journey-leg-header';

                const legRoute = document.createElement('div');
                legRoute.className = 'journey-leg-route';
                const routeStrong = document.createElement('strong');
                // Apply the map colour to the route badge background
                routeStrong.style.background = legColor;
                routeStrong.textContent = `${modeIcon} ${modeLabel} ${route}`;
                legRoute.appendChild(routeStrong);

                // Info button
                const infoBtn = document.createElement('button');
                infoBtn.className = 'journey-leg-info-btn';
                infoBtn.setAttribute('aria-label', `Info for ${route}`);
                infoBtn.textContent = 'ℹ️';
                infoBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    openLegDetailsModal(leg, modeLabel, operatorName, route);
                });

                legHeader.appendChild(legRoute);
                legHeader.appendChild(infoBtn);
                li.appendChild(legHeader);

                // Compact stops display
                const stopsDiv = document.createElement('div');
                stopsDiv.className = 'journey-leg-stops';
                stopsDiv.textContent = `${leg.origin_stop_name || leg.from_stop || leg.origin_stop_id || ''} → ${leg.destination_stop_name || leg.to_stop || leg.destination_stop_id || ''}`;
                li.appendChild(stopsDiv);

                // Times
                const timesDiv = document.createElement('div');
                timesDiv.className = 'journey-leg-times';

                if (leg.departure_time) {
                    const depSpan = document.createElement('span');
                    depSpan.className = 'journey-leg-depart';
                    depSpan.textContent = leg.departure_time;
                    timesDiv.appendChild(depSpan);
                }
                if (leg.departure_time && leg.arrival_time) {
                    const sepSpan = document.createElement('span');
                    sepSpan.className = 'journey-leg-sep';
                    sepSpan.textContent = ' → ';
                    timesDiv.appendChild(sepSpan);
                }
                if (leg.arrival_time) {
                    const arrSpan = document.createElement('span');
                    arrSpan.className = 'journey-leg-arrive';
                    arrSpan.textContent = leg.arrival_time;
                    timesDiv.appendChild(arrSpan);
                }

                // Live delay badge
                const isLiveSource = leg.delay_source === 'live_position' || leg.delay_source === 'darwin' || leg.delay_source === 'route_estimate';
                const isDarwin = leg.delay_source === 'darwin';
                const isRouteEstimate = leg.delay_source === 'route_estimate';

                if (leg.is_cancelled) {
                    const cancelBadge = document.createElement('span');
                    cancelBadge.className = 'journey-leg-delay-badge';
                    cancelBadge.textContent = '❌ Cancelled';
                    cancelBadge.title = leg.cancel_reason || 'This service has been cancelled';
                    timesDiv.appendChild(cancelBadge);
                } else if (leg.estimated_delay_mins != null && leg.estimated_delay_mins > 1 && isLiveSource) {
                    const delayBadge = document.createElement('span');
                    delayBadge.className = 'journey-leg-delay-badge';
                    const src = isDarwin ? 'Darwin live feed' : isRouteEstimate ? 'nearby vehicle on route' : 'live vehicle position data';
                    delayBadge.textContent = `⚠️ ~${leg.estimated_delay_mins} min delay`;
                    delayBadge.title = `Estimated from ${src}`;
                    timesDiv.appendChild(delayBadge);
                    if (leg.delay_reason) {
                        const reasonEl = document.createElement('span');
                        reasonEl.className = 'journey-leg-delay-reason';
                        reasonEl.textContent = leg.delay_reason;
                        timesDiv.appendChild(reasonEl);
                    }
                } else if (isLiveSource && leg.estimated_delay_mins != null) {
                    const onTimeBadge = document.createElement('span');
                    onTimeBadge.className = 'journey-leg-ontime-badge';
                    onTimeBadge.textContent = '✅ On time';
                    onTimeBadge.title = isDarwin
                        ? 'Confirmed on time by National Rail'
                        : isRouteEstimate
                            ? 'Based on nearby vehicle currently on this route'
                            : 'Vehicle is on schedule based on live position';
                    timesDiv.appendChild(onTimeBadge);
                } else if (leg.delay_source === 'schedule') {
                    const schedBadge = document.createElement('span');
                    schedBadge.className = 'journey-leg-schedule-badge';
                    schedBadge.textContent = '📅 Scheduled';
                    schedBadge.title = 'No live data available — times are from the timetable';
                    timesDiv.appendChild(schedBadge);
                }

                // Rail-specific: show platform number
                if (isDarwin && leg.platform) {
                    const platBadge = document.createElement('span');
                    platBadge.className = 'journey-leg-platform-badge';
                    platBadge.textContent = `Platform ${leg.platform}`;
                    timesDiv.appendChild(platBadge);
                }

                li.appendChild(timesDiv);
            }
            // Bidirectional hover linking: store leg reference and wire up
            // mouseenter/mouseleave to widen/restore the map polyline.
            li._leg = leg;
            const isWalkLeg = (leg.mode || 'bus') === 'walk';
            const normalWeight = isWalkLeg ? 3 : 4;
            const hoverWeight = isWalkLeg ? 5 : 7;
            li.addEventListener('mouseenter', () => {
                const pline = legToPolylineMap.get(leg);
                if (pline) pline.setStyle({ weight: hoverWeight });
            });
            li.addEventListener('mouseleave', () => {
                const pline = legToPolylineMap.get(leg);
                if (pline) pline.setStyle({ weight: normalWeight });
            });
            ul.appendChild(li);
        });
        legsEl.appendChild(ul);
        card.appendChild(legsEl);

        // Toggle expand/collapse on header click
        header.addEventListener('click', () => {
            // Collapse all other journeys
            document.querySelectorAll('.journey-card').forEach((otherCard) => {
                const otherLegs = otherCard.querySelector('.journey-legs');
                const otherToggle = otherCard.querySelector('.journey-toggle-icon');
                if (otherCard !== card) {
                    if (otherLegs) otherLegs.style.display = 'none';
                    if (otherToggle) otherToggle.textContent = '▶';
                }
            });

            // Toggle current journey
            const isExpanded = legsEl.style.display === 'block';
            legsEl.style.display = isExpanded ? 'none' : 'block';
            toggleIcon.textContent = isExpanded ? '▶' : '▼';

            if (!isExpanded) {
                drawJourneyOnMap(j);
            }
        });

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
    const departureDateInput = (document.getElementById('departureDate') || {}).value || '';
    const departureTimeInput = (document.getElementById('departureTime') || {}).value || '';
    const timeType = document.getElementById('timeType').value;

    // Ensure typed inputs are resolved to stops if possible
    await ensureSelectedFromInput(true);
    await ensureSelectedFromInput(false);

    // Resolve any typed (un-picked) intermediate stop inputs
    const stopCount = document.querySelectorAll('.stop-input-row').length;
    for (let i = 0; i < stopCount; i++) {
        await ensureStopResolved(i);
    }

    /**
     * Build an origin/destination sub-object from a location item.
     * Returns partial body fields (origin_* or destination_*) with the
     * given prefix ('origin' or 'destination').
     */
    function locationToBodyFields(item, prefix, rawInput) {
        if (item && item.stop_id) return { [`${prefix}_stop_id`]: item.stop_id };
        if (item) {
            const c = extractLatLng(item);
            if (c) return { [`${prefix}_lat`]: c[0], [`${prefix}_lon`]: c[1] };
        }
        if (rawInput) {
            const parts = rawInput.split(',').map(s => s.trim());
            if (parts.length === 2) {
                const la = parseFloat(parts[0]), lo = parseFloat(parts[1]);
                if (!isNaN(la) && !isNaN(lo)) return { [`${prefix}_lat`]: la, [`${prefix}_lon`]: lo };
            }
        }
        return null;
    }

    const originFields = locationToBodyFields(selectedStartItem, 'origin', fromInput && fromInput.value);
    const destFields = locationToBodyFields(selectedEndItem, 'destination', toInput && toInput.value);

    // Basic validation
    if (!originFields || !destFields) {
        routeContent.innerHTML = `<p class="journey-no-results">${t.selectValidPoints}</p>`;
        return;
    }

    // Collect active stops (only resolved ones)
    const activeStops = [];
    for (let i = 0; i < stopCount; i++) {
        if (stopItems[i]) activeStops.push(stopItems[i]);
    }

    // Build common request parameters
    const commonParams = { preference: pathfinding, walking_speed: walkingSpeed, arrive_by: (timeType === 'arrive-before') };
    if (timeType === 'now') {
        const current = getExactCurrentDepartureSelection();
        commonParams.departure_date = current.date;
        commonParams.departure_time = current.time;
        commonParams.arrive_by = false;
    } else {
        if (departureDateInput) commonParams.departure_date = departureDateInput;
        if (departureTimeInput) commonParams.departure_time = departureTimeInput;
    }

    routeContent.innerHTML = `<p>${t.planningRoute}</p>`;

    /**
     * Call /journey/plan for a single origin→destination pair.
     */
    async function planSegment(originF, destinationF) {
        const body = Object.assign({}, originF, destinationF, commonParams);
        const res = await fetch(`${apiUrl}/journey/plan`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            const txt = await res.text();
            throw new Error(`Server returned ${res.status}: ${txt}`);
        }
        return res.json();
    }

    try {
        let allJourneys;
        let departureDate = null;

        if (activeStops.length === 0) {
            // Simple point-to-point query (original behaviour)
            const data = await planSegment(originFields, destFields);
            allJourneys = data.journeys || [];
            departureDate = data.departure_date || null;

            // Fetch destination weather and show it
            fetchAndShowDestinationWeather(selectedEndItem, data.destination);
        } else {
            // Multi-leg: chain segments origin → stop[0] → stop[1] → … → dest
            const waypoints = [
                { item: selectedStartItem, rawInput: fromInput && fromInput.value },
                ...activeStops.map(it => ({ item: it, rawInput: null })),
                { item: selectedEndItem, rawInput: toInput && toInput.value },
            ];

            // Request journeys for each segment in sequence, taking the best (first) result
            const segmentJourneys = [];
            let currentTime = commonParams.departure_time;
            let currentDate = commonParams.departure_date;

            for (let seg = 0; seg < waypoints.length - 1; seg++) {
                const from = waypoints[seg];
                const to = waypoints[seg + 1];
                const oF = locationToBodyFields(from.item, 'origin', from.rawInput);
                const dF = locationToBodyFields(to.item, 'destination', to.rawInput);
                if (!oF || !dF) {
                    throw new Error(`Could not resolve waypoint between "${getLabelFromItem(from.item) || 'stop'}" and "${getLabelFromItem(to.item) || 'stop'}"`);
                }
                const segParams = Object.assign({}, commonParams, { departure_time: currentTime, departure_date: currentDate });
                const body = Object.assign({}, oF, dF, segParams);
                const res = await fetch(`${apiUrl}/journey/plan`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (!res.ok) {
                    const txt = await res.text();
                    throw new Error(`Server returned ${res.status}: ${txt}`);
                }
                const segData = await res.json();
                if (!departureDate) departureDate = segData.departure_date || null;
                const segJourneys = segData.journeys || [];
                if (segJourneys.length === 0) {
                    const fromName = getLabelFromItem(from.item) || `waypoint ${seg + 1}`;
                    const toName = getLabelFromItem(to.item) || `waypoint ${seg + 2}`;
                    throw new Error(`No routes found between "${fromName}" and "${toName}"`);
                }
                const bestSeg = segJourneys[0];
                segmentJourneys.push(bestSeg);

                // Advance departure time to arrival of this segment (+ 5 min transfer buffer + user dwell time)
                const segLegs = bestSeg.legs || [];
                const lastLeg = segLegs[segLegs.length - 1];
                if (lastLeg && lastLeg.arrival_time) {
                    // Parse HH:MM, add 5 minutes transfer buffer and any user-specified dwell time
                    // seg corresponds to waypoints[seg]→waypoints[seg+1]; intermediate stops are
                    // activeStops[0..activeStops.length-1], so dwell applies when seg < activeStops.length
                    const dwellMins = (seg < activeStops.length) ? (parseInt(stopDwellTimes[seg], 10) || 0) : 0;
                    const [hh, mm] = lastLeg.arrival_time.split(':').map(Number);
                    if (!isNaN(hh) && !isNaN(mm)) {
                        const totalMins = hh * 60 + mm + 5 + dwellMins;
                        const nh = Math.floor(totalMins / 60) % 24;
                        const nm = totalMins % 60;
                        currentTime = `${String(nh).padStart(2, '0')}:${String(nm).padStart(2, '0')}`;
                    } else {
                        currentTime = lastLeg.arrival_time;
                    }
                    if (segData.departure_date) currentDate = segData.departure_date;
                }
            }

            // Combine all segment legs into one merged journey
            const mergedLegs = segmentJourneys.flatMap(seg => seg.legs || []);
            allJourneys = [{ legs: mergedLegs }];

            // Fetch destination weather for the final destination
            fetchAndShowDestinationWeather(selectedEndItem, null);
        }

        renderJourneyList(allJourneys, departureDate);

        // Auto-collapse planner
        const routePlanner = document.querySelector('.route-planner');
        const plannerToggle = document.getElementById('plannerToggle');
        if (!routePlanner.classList.contains('collapsed')) {
            routePlanner.classList.add('collapsed');
            plannerToggle.setAttribute('aria-expanded', 'false');
            setTimeout(() => {
                if (map) map.invalidateSize();
            }, 320);
        }
    } catch (err) {
        console.error('Plan route error:', err);
        routeContent.innerHTML = `<p class="journey-no-results">${t.routePlanningError}: ${err.message}</p>`;
        showNotification(t.routePlanningFailed);
        clearRouteLayers();
    }
});

/**
 * Fetch weather for the journey destination and inject a badge into the
 * route-display header.
 *
 * @param {object|null} destItem  - The selected destination item (may have lat/lon)
 * @param {object|null} destData  - The destination object from the API response ({stop_id, name})
 */
async function fetchAndShowDestinationWeather(destItem, destData) {
    // Remove any previous destination weather badge
    const old = document.getElementById('destWeatherBadge');
    if (old) old.remove();

    if (typeof fetchWeatherData !== 'function') return;

    let lat = null, lon = null;
    if (destItem) {
        const c = extractLatLng(destItem);
        if (c) { lat = c[0]; lon = c[1]; }
    }
    if (lat == null || lon == null) return;

    const data = await fetchWeatherData(lat, lon);
    if (!data || !data.weather) return;

    const wd = data.weather;
    const temp = Math.round(wd.main.temp);
    const icon = wd.weather[0].icon;
    const desc = wd.weather[0].description;
    const cityName = (destData && destData.name) || wd.name || '';

    const badge = document.createElement('div');
    badge.id = 'destWeatherBadge';
    badge.className = 'journey-dest-weather';
    badge.title = `Weather at ${cityName || 'destination'}: ${desc}`;
    badge.innerHTML = `
        <img src="https://openweathermap.org/img/wn/${icon}.png" alt="${desc}">
        <span>${temp}°C at ${cityName || 'destination'}</span>
    `;

    // Insert after the "Route Information" h3
    const h3 = document.querySelector('.route-display h3');
    if (h3) h3.insertAdjacentElement('afterend', badge);
}

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
