// Weather API for North Lancashire
// Using OpenWeatherMap API - Get your free API key from https://openweathermap.org/api

const WEATHER_CONFIG = {
    apiKey: '178a080c9e780ddc93f4f4a9153f93c1', // Replace with your OpenWeatherMap API key
    // North Lancashire coordinates (centered around Lancaster)
    latitude: 54.0465,
    longitude: -2.8011,
    city: 'Lancaster, UK'
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
    const weatherWidget = document.getElementById('weatherWidget');
    
    try {
        // Call local proxy to avoid browser CORS restrictions
        const apiUrl = `/weather?lat=${WEATHER_CONFIG.latitude}&lon=${WEATHER_CONFIG.longitude}`;
        
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
