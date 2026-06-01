# RedGW — Redis Gateway (Lite)

A **Redis-backed REST gateway** for sharing data across heterogeneous systems (e.g. HRM, ERP, CRM) — with **per-namespace authorization and an audit trail built in**.

RedGW turns Redis into a *governed* data-sharing bus for internal, often air-gapped, networks: independent systems exchange data through a standardized REST API, each client gets scoped read/write permissions per namespace, and every access is logged. Redis stays the in-memory hub; RedGW makes it safe to expose across teams that don't trust each other by default.

> **Open-source edition (lite)** — [Apache License 2.0](LICENSE). Includes the common gateway features:
> Redis REST API, WebSocket Pub/Sub, authentication & authorization, audit logging, and monitoring.
> Commercial database-integration features are full-version-only and are **not** part of this repository.

## Why RedGW? (vs. a thin Redis-to-HTTP proxy or raw Redis)

If you only need to call Redis commands over HTTP, a thin proxy like [Webdis](https://github.com/nicolasff/webdis) is smaller and faster, and exposing Redis directly is simpler still. RedGW solves a different problem: **sharing one Redis hub across multiple independent systems that should not see each other's data by default.**

- **Per-namespace authorization** — each client (system) is granted read/write only on the namespaces it should see; it cannot touch another's data unless explicitly allowed.
- **Audit trail** — who read or wrote what, and when, is logged. Useful for internal compliance and incident review.
- **Resource-oriented REST** — Redis' six data structures are exposed as curated REST resources (KV, Map, Queue, Group, Rank, Event) instead of raw commands, so integrating systems need no Redis-command knowledge.
- **Batteries included** — TLS termination, rate limiting, and a Prometheus/Grafana monitoring stack ship in the compose file.

In short, RedGW trades raw throughput and full command coverage for **governance, isolation, and operability**. If you don't need those, you probably don't need RedGW.

## Features

Exposes Redis' six core data structures as a REST API.

| Type | Redis structure | Example use |
|------|-----------------|-------------|
| **KV** | String | Simple key/value, distributed locks (SETNX), counters |
| **Map** | Hash | Structured records (orders, user profiles) |
| **Queue** | List | Job queues, message buffers |
| **Group** | Set | Tags, membership, set operations (union/intersection/difference) |
| **Rank** | Sorted Set | Leaderboards, score-based lookups |
| **Event** | Stream | Event sourcing, Consumer Group subscriptions |

It also provides **Pub/Sub** (including WebSocket subscriptions) and an **Admin API** (key management, metrics, client lookup).

```
Client ──TLS──▶ Nginx (443) ──▶ RedGW (8080) ──▶ Redis (6379)
                TLS termination    API-key auth      in-memory
                rate limiting      NS authorization  allkeys-lru
```

## Quick start

### Prerequisites

- Docker & Docker Compose

### Install and run

```bash
# 1. Configure environment variables
cp .env.example .env
# Edit .env and set REDIS_PASSWORD, API keys, etc. to real values

# 2. Generate a TLS certificate (for development)
mkdir -p nginx/certs
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout nginx/certs/server.key -out nginx/certs/server.crt \
  -subj "/CN=redgw.internal"

# 3. Build and run
bash build.sh
docker compose up -d

# 4. Health check
curl -k https://localhost:3443/health
```

### API examples

```bash
# Store a KV value
curl -k -X PUT https://localhost:3443/api/v1/ns/shared/kv/greeting \
  -H "X-API-Key: <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"value": "hello"}'

# Read a KV value
curl -k https://localhost:3443/api/v1/ns/shared/kv/greeting \
  -H "X-API-Key: <YOUR_API_KEY>"

# Store a Map
curl -k -X PUT https://localhost:3443/api/v1/ns/shared/map/user:001 \
  -H "X-API-Key: <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"fields": {"name": "Alice", "dept": "sales"}}'
```

## Authentication & authorization

Every API request is authenticated with the `X-API-Key` header. Each client is granted per-namespace
read/write permissions.

```yaml
# config/config.yaml
clients:
  - id: hrm
    api_key: ${REDGW_CLIENT_HRM_API_KEY}
    namespaces:
      HRM: [read, write]       # own namespace
      shared: [read, write]    # shared namespace
      ERP: [read]              # read-only access to another system
```

## Docker Compose services

| Service | Image | Host→container port | Description |
|---------|-------|---------------------|-------------|
| **redis** | redis:7.2.14-alpine | 6379 (internal) | In-memory data store (256MB, LRU) |
| **redgw** | redgw-lite:0.1.2 | 8080 (internal) | REST API server (Gunicorn × 4) |
| **nginx** | nginx:1.29.5-alpine | **3443→443**, **3080→8080** | TLS termination, reverse proxy |
| **prometheus** | prom/prometheus:v3.9.1 | 9090 (internal) | Metrics collection (7-day retention) |
| **grafana** | grafana/grafana:12.3.3 | **3300→3000** | Monitoring dashboard |
| **redisinsight** | redis/redisinsight:3.0.3 | **3540→5540** | Redis management tool (development) |

> Externally exposed host ports all use the 3000 range. Container-internal ports keep their standard values.

## Tests

```bash
# Full test suite (run inside the Docker container)
bash -c 'source .env && docker compose run --rm \
  -e "REDGW_TEST_REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/15" \
  -e "REDGW_REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/15" \
  redgw python -m pytest tests/ -v'
```

## Project layout

```
RedGW-lite/
├── app/                    # Main application
│   ├── routers/            #   API endpoints (kv, map, queue, group, rank, event, pubsub, admin)
│   ├── schemas/            #   Pydantic V2 request/response models
│   ├── auth/               #   Authentication & authorization
│   ├── audit/              #   Audit logging
│   └── utils/              #   Utilities (automatic Rust/Python fallback)
├── redgw_core/             # Rust (PyO3) performance module
│   └── src/                #   key_builder, validation
├── config/                 # Configuration files (YAML)
├── tests/                  # Tests (pytest + shell)
├── nginx/                  # Nginx config & TLS
├── prometheus/             # Prometheus scrape config
├── grafana/                # Grafana dashboards & provisioning
├── docker-compose.yml
├── Dockerfile              # Multi-stage (Rust → Python 3.14-slim)
└── build.sh
```

## Tech stack

- **Runtime**: Python 3.14, FastAPI 0.136, Gunicorn 25.3 + Uvicorn 0.44
- **Rust core**: redgw_core (PyO3) — accelerates key/value *validation* only, **not** the Redis I/O path. Throughput is bound by Python/FastAPI, so a thin C proxy will out-throughput RedGW; the Rust module is about validation cost and consistency, not headline performance. Falls back to Python automatically if the native module fails to load.
- **Storage**: Redis 7.2.14 (hiredis), Pydantic 2.13
- **Proxy**: Nginx 1.29.5 (TLS 1.2/1.3, rate limiting, WebSocket proxy)
- **Monitoring**: Prometheus v3.9.1 + Grafana 12.3.3

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE) — see [NOTICE](NOTICE) for details.
