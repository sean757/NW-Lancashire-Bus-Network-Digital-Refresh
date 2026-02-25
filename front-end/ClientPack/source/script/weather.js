// Weather API for North Lancashire
// Using OpenWeatherMap API - Get your free API key from https://openweathermap.org/api

const WEATHER_CONFIG = {
    // North Lancashire coordinates (centered around Lancaster)
    latitude: 53.77838,
    longitude: -2.71330,
    city: 'Preston, UK'
};

// Initialize weather widget on page load
document.addEventListener('DOMContentLoaded', () => {
    fetchWeather();
    // Update weather every 30 minutes
    setInterval(fetchWeather, 30 * 60 * 1000);
});

/**
 * Fetch weather data from OpenWeatherMap API
 */
async function fetchWeather() {
    try {
        // Call local proxy to avoid browser CORS restrictions
        const apiUrl = `http://localhost:8080/api/v1/weather?lat=${WEATHER_CONFIG.latitude}&lon=${WEATHER_CONFIG.longitude}`;

        const response = await fetch(apiUrl);

        if (!response.ok) {
            throw new Error(`Status ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();
        displayWeather(data);

    } catch (error) {
        console.error('Detailed Error:', error);
        displayWeatherError(`Error: ${error.message}`);
    }
}

/**
 * Display weather data in the widget
 */
function displayWeather(data) {
    const weatherWidget = document.getElementById('weatherWidget');

    // Extract weather data from the nested structure
    const weatherData = data.weather;
    const temperature = Math.round(weatherData.main.temp);
    const description = weatherData.weather[0].description;
    const feelsLike = Math.round(weatherData.main.feels_like);
    const weatherIcon = weatherData.weather[0].icon;

    weatherWidget.innerHTML = `
        <div class="weather-header">
            <img src="https://openweathermap.org/img/wn/${weatherIcon}@2x.png" 
                 alt="${description}" 
                 class="weather-icon">
            <div class="weather-temp">
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
