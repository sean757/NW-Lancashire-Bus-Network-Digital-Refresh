-- SCC200 Transport Database Schema
-- Run: psql -h host.containers.internal -U transport -d transport_db -f scripts/init_db.sql

-- Enable PostGIS if available (optional, for geospatial queries)
-- CREATE EXTENSION IF NOT EXISTS postgis;

-- ============================================
-- STOPS
-- ============================================
CREATE TABLE IF NOT EXISTS stops (
    stop_id         VARCHAR(20) PRIMARY KEY,       -- NaPTAN ATCOCode
    stop_name       VARCHAR(255) NOT NULL,
    locality        VARCHAR(255),
    bearing         VARCHAR(5),                     -- N, NE, E, SE, S, SW, W, NW
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    stop_type       VARCHAR(20) DEFAULT 'bus',      -- bus, rail, tram
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stops_locality ON stops(locality);
CREATE INDEX IF NOT EXISTS idx_stops_coords ON stops(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_stops_name ON stops USING gin(to_tsvector('english', stop_name));

-- ============================================
-- ROUTES
-- ============================================
CREATE TABLE IF NOT EXISTS routes (
    route_id        VARCHAR(50) PRIMARY KEY,
    route_name      VARCHAR(255) NOT NULL,          -- e.g. "40", "X1"
    operator        VARCHAR(100),
    description     TEXT,
    route_type      VARCHAR(20) DEFAULT 'bus',
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ============================================
-- ROUTE STOPS (ordered stops per route)
-- ============================================
CREATE TABLE IF NOT EXISTS route_stops (
    id              SERIAL PRIMARY KEY,
    route_id        VARCHAR(50) NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
    stop_id         VARCHAR(20) NOT NULL REFERENCES stops(stop_id) ON DELETE CASCADE,
    stop_sequence   INTEGER NOT NULL,
    direction       VARCHAR(10) DEFAULT 'outbound', -- outbound, inbound
    UNIQUE(route_id, stop_id, direction, stop_sequence)
);

CREATE INDEX IF NOT EXISTS idx_route_stops_route ON route_stops(route_id);
CREATE INDEX IF NOT EXISTS idx_route_stops_stop ON route_stops(stop_id);

-- ============================================
-- TIMETABLES
-- ============================================
CREATE TABLE IF NOT EXISTS timetables (
    id              SERIAL PRIMARY KEY,
    route_id        VARCHAR(50) NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
    stop_id         VARCHAR(20) NOT NULL REFERENCES stops(stop_id) ON DELETE CASCADE,
    trip_id         VARCHAR(100) NOT NULL,
    arrival_time    TIME NOT NULL,
    departure_time  TIME NOT NULL,
    stop_sequence   INTEGER NOT NULL,
    direction       VARCHAR(10) DEFAULT 'outbound',
    days_of_week    INTEGER DEFAULT 127,            -- bitmask: Mon=1, Tue=2, Wed=4, Thu=8, Fri=16, Sat=32, Sun=64
    valid_from      DATE,
    valid_until     DATE
);

CREATE INDEX IF NOT EXISTS idx_timetables_route ON timetables(route_id);
CREATE INDEX IF NOT EXISTS idx_timetables_stop ON timetables(stop_id);
CREATE INDEX IF NOT EXISTS idx_timetables_trip ON timetables(trip_id);
CREATE INDEX IF NOT EXISTS idx_timetables_time ON timetables(departure_time);

-- ============================================
-- LIVE POSITIONS
-- ============================================
CREATE TABLE IF NOT EXISTS live_positions (
    id              SERIAL PRIMARY KEY,
    vehicle_id      VARCHAR(50) NOT NULL,
    route_id        VARCHAR(50) REFERENCES routes(route_id),
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    bearing         DOUBLE PRECISION,
    speed           DOUBLE PRECISION,
    delay_seconds   INTEGER DEFAULT 0,
    source          VARCHAR(20) DEFAULT 'stomp',    -- stomp, api, manual
    recorded_at     TIMESTAMP NOT NULL,
    received_at     TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_live_positions_vehicle ON live_positions(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_live_positions_route ON live_positions(route_id);
CREATE INDEX IF NOT EXISTS idx_live_positions_time ON live_positions(received_at);

-- Keep only recent positions (partition or cleanup via cron)
-- Recommended: DELETE FROM live_positions WHERE received_at < NOW() - INTERVAL '24 hours'

-- ============================================
-- DISRUPTIONS
-- ============================================
CREATE TABLE IF NOT EXISTS disruptions (
    id              SERIAL PRIMARY KEY,
    route_id        VARCHAR(50) REFERENCES routes(route_id),
    stop_id         VARCHAR(20) REFERENCES stops(stop_id),
    disruption_type VARCHAR(50) NOT NULL,           -- delay, cancellation, diversion, stop_closure
    severity        VARCHAR(20) DEFAULT 'minor',    -- minor, moderate, severe
    title           VARCHAR(255) NOT NULL,
    description     TEXT,
    affected_area   VARCHAR(255),
    detected_at     TIMESTAMP DEFAULT NOW(),
    resolved_at     TIMESTAMP,
    source          VARCHAR(50) DEFAULT 'auto',     -- auto (from delay detection), manual, feed
    active          BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_disruptions_active ON disruptions(active) WHERE active = TRUE;
CREATE INDEX IF NOT EXISTS idx_disruptions_route ON disruptions(route_id);
CREATE INDEX IF NOT EXISTS idx_disruptions_type ON disruptions(disruption_type);

-- ============================================
-- DELAY STATISTICS (aggregated for analysis)
-- ============================================
CREATE TABLE IF NOT EXISTS delay_statistics (
    id              SERIAL PRIMARY KEY,
    route_id        VARCHAR(50) NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
    stop_id         VARCHAR(20) NOT NULL REFERENCES stops(stop_id) ON DELETE CASCADE,
    date            DATE NOT NULL,
    hour            INTEGER NOT NULL CHECK (hour >= 0 AND hour <= 23),
    avg_delay_secs  DOUBLE PRECISION,
    max_delay_secs  INTEGER,
    sample_count    INTEGER DEFAULT 0,
    UNIQUE(route_id, stop_id, date, hour)
);

CREATE INDEX IF NOT EXISTS idx_delay_stats_date ON delay_statistics(date);
CREATE INDEX IF NOT EXISTS idx_delay_stats_route ON delay_statistics(route_id);

--Rail Info
CREATE TABLE rail_stops (
    atco_code TEXT PRIMARY KEY,
    common_name TEXT,
    stop_type TEXT,
    longitude DOUBLE PRECISION,
    latitude DOUBLE PRECISION
);

CREATE TABLE rail_corpus (
    stanox TEXT PRIMARY KEY,
    tiploc TEXT,
    crs_code TEXT,
    description TEXT
);