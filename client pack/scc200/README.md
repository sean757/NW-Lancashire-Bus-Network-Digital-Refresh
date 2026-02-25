# SCC200FrontEnd

## Run with local weather proxy

1. Install dependencies:
	```bash
	npm install
	```
2. Start the server:
	```bash
	npm start
	```
3. Open `http://localhost:3000` in your browser.

The frontend now requests weather data from `/weather`, and the Node server proxies that request to `https://transport.scc.lancs.ac.uk/weather`.