Instructions for starting the program.

--- Pre-Start Checklist ---

1. Ensure API urls correct

Sometimes we change API URLs on the client side when we are using proxy servers to make the server available via the internet. These should have been changed over
for submissions but first verify this is correct:

Go to front-end/ClientPack/source/script/app.js and make sure the apiURL variable on line 2 is set to 'https://localhost:8080/api/v1'
Go to front-end/ClientPack/source/script/weather.js and make sure the URLs on lines 89 and 63 are set to `https://localhost:8080/api/v1/weather?lat=${lat}&lon=${lon}`

2. Ensure required software is installed

The only piece of software that is required for this is containerisation software. On lab machines this should be podman but docker works for this too.

3. Ensure you are on the University Network or using the University VPN

You must ensure you are using the University Network or VPN or the backend will not be able to access the endpoints used by the server.

4. Ensure VS Code is installed

You must ensure that the machine you are using has VS code installed to manage the devcontainer of the backend server

--- Step 1: Initialise DB container ---

This step covers the initialisation of the database container

1. In a terminal on the lab machine run: podman run -d --name scc200-db -e POSTGRES_USER=transport -e POSTGRES_PASSWORD=transport_dev -e POSTGRES_DB=transport_db -p 5432:5432 docker.io/library/postgres:16



--- Step 2: Open the project folder ---

This step covers opening the project into VS Code and ensuring the dev 