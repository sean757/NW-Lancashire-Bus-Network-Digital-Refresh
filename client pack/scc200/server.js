const express = require('express');

const app = express();
const PORT = process.env.PORT || 3000;
const WEATHER_UPSTREAM = 'https://transport.scc.lancs.ac.uk/weather';

app.use(express.static(__dirname));

app.get('/weather', async (req, res) => {
  const lat = req.query.lat || '54.05';
  const lon = req.query.lon || '-2.80';

  try {
    const upstreamUrl = `${WEATHER_UPSTREAM}?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`;
    const upstreamResponse = await fetch(upstreamUrl);

    if (!upstreamResponse.ok) {
      return res.status(upstreamResponse.status).json({
        error: `Upstream error ${upstreamResponse.status}: ${upstreamResponse.statusText}`
      });
    }

    const data = await upstreamResponse.json();
    res.json(data);
  } catch (error) {
    res.status(502).json({
      error: 'Failed to fetch weather data',
      details: error.message
    });
  }
});

app.listen(PORT, '0.0.0.0', () => {
  // Use the IP address we found in your ipconfig (10.241.52.45)
  console.log(`Server running!`);
  console.log(`Local: http://localhost:${PORT}`);
  console.log(`Phone: http://10.241.52.45:${PORT}`);
});

