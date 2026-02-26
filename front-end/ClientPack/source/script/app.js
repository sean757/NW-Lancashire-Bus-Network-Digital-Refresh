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

// Map / state variables
let map = null;
let startMarker = null;
let endMarker = null;

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

    // Note: Map click handling for markers will be implemented in a future update
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

planRouteBtn.addEventListener('click', () => {
    const startPoint = document.getElementById('startPoint').value;
    const endPoint = document.getElementById('endPoint').value;
    const pathfinding = document.getElementById('pathfinding').value;
    const walkingSpeed = document.getElementById('walkingSpeed').value;

    if (!startPoint || !endPoint) {
        routeContent.innerHTML = '<p style="color: #e74c3c;">Please click on the map to select both start and end points.</p>';
        return;
    }

    // Generate sample route information (in a real app, this would call an API)
    const pathfindingText = pathfinding === 'fastest' ? 'Fastest Time' : 'Least Number of Changes';
    const speedText = walkingSpeed.charAt(0).toUpperCase() + walkingSpeed.slice(1);

    routeContent.innerHTML = `
        <div style="margin-bottom: 15px;">
            <strong style="color: #2E5090;">Route Preferences:</strong>
            <p style="margin: 5px 0;">Pathfinding: ${pathfindingText}</p>
            <p style="margin: 5px 0;">Walking Speed: ${speedText}</p>
        </div>
        <div style="margin-bottom: 15px;">
            <strong style="color: #2E5090;">Journey Summary:</strong>
            <p style="margin: 5px 0;">From: ${startPoint}</p>
            <p style="margin: 5px 0;">To: ${endPoint}</p>
        </div>
        <div style="padding: 10px; background-color: #E8F5E9; border-left: 4px solid #27AE60; border-radius: 4px;">
            <p style="margin: 0; color: #27AE60; font-weight: 500;">✓ Route Ready</p>
            <p style="margin: 5px 0 0 0; font-size: 14px;">Your route has been calculated based on your preferences.</p>
        </div>
    `;

    // Auto-collapse the planner after planning route
    const routePlanner = document.querySelector('.route-planner');
    const plannerToggle = document.getElementById('plannerToggle');
    if (!routePlanner.classList.contains('collapsed')) {
        routePlanner.classList.add('collapsed');
        plannerToggle.setAttribute('aria-expanded', 'false');
    }

    // Note: Route drawing will be implemented when marker functionality is added
    // drawRouteLine();
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
