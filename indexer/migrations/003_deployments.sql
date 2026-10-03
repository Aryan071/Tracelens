-- Deployment records: which commit a service version was built from.
-- Rows come from POST /deployments (source 'api'). Versions that appear in telemetry but were
-- never registered are discovered from ClickHouse at query time and are not stored here.
CREATE TABLE deployments (
    id          bigserial PRIMARY KEY,
    service     text NOT NULL,
    version     text NOT NULL,
    commit_sha  text NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{7,40}$'),
    repo        text,
    deployed_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (service, version)
);
CREATE INDEX deployments_service_idx ON deployments (service, deployed_at);
