// Leaflet map and markers
let map = null;
let startMarker = null;
let endMarker = null;

 
 
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

// Accessible notification function
function showNotification(message) {
    const toast = document.getElementById('notificationToast');
    toast.textContent = message;
    toast.classList.add('show');
    
    setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}
