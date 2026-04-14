FROM python:3.12-bookworm

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client \
        wget \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install Java 21 (Eclipse Temurin)
RUN wget -qO- https://packages.adoptium.net/artifactory/api/gpg/key/public | tee /usr/share/keyrings/adoptium.asc \
    && echo "deb [signed-by=/usr/share/keyrings/adoptium.asc] https://packages.adoptium.net/artifactory/deb bookworm main" \
       > /etc/apt/sources.list.d/adoptium.list \
    && apt-get update && apt-get install -y --no-install-recommends temurin-21-jre \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Install Python dependencies first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 8080

# Default: run the start-inside-docker helper
CMD ["bash", "docker-start.sh"]
