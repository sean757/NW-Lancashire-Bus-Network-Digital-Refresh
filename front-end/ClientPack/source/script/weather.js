// Weather API for North Lancashire
// Using OpenWeatherMap API - Get your free API key from https://openweathermap.org/api

// Fallback coordinates (Lancaster) used when geolocation is unavailable
const WEATHER_FALLBACK = {
    latitude: 54.0500,
    longitude: -2.8000,
};

// Initialize weather widget on page load — request user location first
document.addEventListener('DOMContentLoaded', () => {
    initWeather();
    // Update weather every 30 minutes
    setInterval(initWeather, 30 * 60 * 1000);
});

/**
 * Request user location then fetch weather. Falls back gracefully.
 */
function initWeather() {
    if ('geolocation' in navigator) {
        navigator.geolocation.getCurrentPosition(
            (position) => {
                fetchWeatherForCoords(position.coords.latitude, position.coords.longitude);
            },
            (error) => {
                let msg;
                switch (error.code) {
                    case error.PERMISSION_DENIED:
                        msg = 'Location access denied. Showing Lancaster weather.';
                        break;
                    case error.POSITION_UNAVAILABLE:
                        msg = 'Location unavailable. Showing Lancaster weather.';
                        break;
                    case error.TIMEOUT:
                        msg = 'Location request timed out. Showing Lancaster weather.';
                        break;
                    default:
                        msg = 'Location error. Showing Lancaster weather.';
                }
                // Show brief error then fall back to default location
                displayWeatherError(msg);
                setTimeout(() => {
                    fetchWeatherForCoords(WEATHER_FALLBACK.latitude, WEATHER_FALLBACK.longitude);
                }, 2000);
            },
            { timeout: 8000, maximumAge: 5 * 60 * 1000 }
        );
    } else {
        // Geolocation not supported — use fallback silently
        fetchWeatherForCoords(WEATHER_FALLBACK.latitude, WEATHER_FALLBACK.longitude);
    }
}

/**
 * Fetch weather data for the given coordinates via the backend proxy.
 * @param {number} lat
 * @param {number} lon
 */
async function fetchWeatherForCoords(lat, lon) {
    try {
        // Call local proxy to avoid browser CORS restrictions
        const url = `https://naoma-veinal-adelina.ngrok-free.dev/api/v1/weather?lat=${lat}&lon=${lon}`;

        const response = await fetch(url);

        if (!response.ok) {
            throw new Error(`Status ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();
        displayWeather(data);

    } catch (error) {
        console.error('Detailed Error:', error);
        displayWeatherError(`Weather unavailable`);
    }
}

/**
 * Fetch weather for given coordinates and return parsed data (used externally).
 * Returns null on failure.
 * @param {number} lat
 * @param {number} lon
 * @returns {Promise<object|null>}
 */
async function fetchWeatherData(lat, lon) {
    try {
        const url = `https://naoma-veinal-adelina.ngrok-free.dev/api/v1/weather?lat=${lat}&lon=${lon}`;
        const response = await fetch(url);
        if (!response.ok) return null;
        return await response.json();
    } catch (error) {
        console.error('fetchWeatherData error:', error);
        return null;
    }
}

/**
 * Display weather data in the widget
 * @param {object} data - API response object
 * @param {string} [locationLabel] - Optional label to display (e.g. city name or "Your location")
 */
function displayWeather(data, locationLabel) {
    const weatherWidget = document.getElementById('weatherWidget');

    // Extract weather data from the nested structure
    const weatherData = data.weather;
    const temperature = Math.round(weatherData.main.temp);
    const description = weatherData.weather[0].description;
    const feelsLike = Math.round(weatherData.main.feels_like);
    const weatherIcon = weatherData.weather[0].icon;
    const cityName = locationLabel || weatherData.name || '';

    weatherWidget.innerHTML = `
        <div class="weather-header">
            <img src="https://openweathermap.org/img/wn/${weatherIcon}@2x.png" 
                 alt="${description}" 
                 class="weather-icon">
            <div class="weather-temp">
                ${cityName ? `<span class="weather-city">${cityName}</span>` : ''}
                <span class="temp-value">${temperature}°C</span>
                <span class="temp-feels">Feels like ${feelsLike}°C</span>
            </div>
        </div>
    `;

    weatherWidget.classList.remove('error');
}

/**
 * Display error message in the widget
 */
function displayWeatherError(message) {
    const weatherWidget = document.getElementById('weatherWidget');
    weatherWidget.innerHTML = `
        <div class="weather-error">
            <p>⚠️ ${message}</p>
        </div>
    `;
    weatherWidget.classList.add('error');
}

/**
 * Capitalize first letter of each word
 */
function capitalizeWords(str) {
    return str.split(' ').map(word =>
        word.charAt(0).toUpperCase() + word.slice(1)
    ).join(' ');
}
