CREATE TABLE IF NOT EXISTS spans
(
    start_time          DateTime64(9, 'UTC') CODEC(Delta, ZSTD),
    duration_ns         UInt64 CODEC(T64, ZSTD),
    trace_id            FixedString(32),
    span_id             FixedString(16),
    parent_span_id      FixedString(16),
    service             LowCardinality(String),
    service_version     LowCardinality(String),
    name                LowCardinality(String),
    kind                Enum8('UNSPECIFIED' = 0, 'INTERNAL' = 1, 'SERVER' = 2, 'CLIENT' = 3, 'PRODUCER' = 4, 'CONSUMER' = 5),
    status_code         Enum8('UNSET' = 0, 'OK' = 1, 'ERROR' = 2),
    status_message      String CODEC(ZSTD),
    attributes          Map(LowCardinality(String), String) CODEC(ZSTD),
    resource_attributes Map(LowCardinality(String), String) CODEC(ZSTD),
    events Nested
    (
        ts         DateTime64(9, 'UTC'),
        name       LowCardinality(String),
        attributes Map(LowCardinality(String), String)
    ),

    -- Hot attributes, materialized at insert so the engine never scans the Map.
    -- Each coalesces the current OTel semantic convention with the legacy name.
    http_route    LowCardinality(String) MATERIALIZED attributes['http.route'],
    http_method   LowCardinality(String) MATERIALIZED coalesce(nullIf(attributes['http.request.method'], ''), attributes['http.method']),
    db_system     LowCardinality(String) MATERIALIZED coalesce(nullIf(attributes['db.system.name'], ''), attributes['db.system']),
    db_statement  String MATERIALIZED coalesce(nullIf(attributes['db.query.text'], ''), attributes['db.statement']) CODEC(ZSTD),
    code_function LowCardinality(String) MATERIALIZED coalesce(nullIf(attributes['code.function.name'], ''), attributes['code.function']),
    code_filepath LowCardinality(String) MATERIALIZED coalesce(nullIf(attributes['code.file.path'], ''), attributes['code.filepath']),
    code_lineno   UInt32 MATERIALIZED toUInt32OrZero(coalesce(nullIf(attributes['code.line.number'], ''), attributes['code.lineno'])),

    INDEX idx_trace_id trace_id TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(start_time)
ORDER BY (service, name, toStartOfHour(start_time), start_time)
TTL toDateTime(start_time) + INTERVAL {retention_days} DAY
SETTINGS ttl_only_drop_parts = 1, non_replicated_deduplication_window = 1000
