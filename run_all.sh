#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
    echo "Error: this script requires bash. Run: bash run_all.sh" >&2
    exit 1
fi
set -e
set -u
set -o pipefail 2>/dev/null || true

# ============================================================================
# run_all.sh — Mass load testing of student projects
#
# Dependencies: Docker only
#
# Usage:
#   1. Fill projects.txt (student name + path to project)
#   2. bash run_all.sh
#
# Layout on VM:
#   /home/user/
#   ├── students/             # student project folders
#   │   ├── ivanov/
#   │   ├── petrov/
#   │   └── sidorov/
#   └── locust-runner/        # this repo
#       ├── Dockerfile
#       ├── locustfile.py
#       ├── run_all.sh
#       └── projects.txt
#
# Results:
#   locust-runner/results/
#   ├── ivanov/
#   │   ├── stats_stats.csv      — Locust CSV stats
#   │   ├── locust_stdout.log    — Locust stdout
#   │   ├── info.json            — /info response
#   │   └── docker_build.log     — build logs (on failure)
#   ├── petrov/
#   │   └── ...
#   └── summary.csv              — summary across all projects
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECTS_FILE="$SCRIPT_DIR/projects.txt"
RESULTS_DIR="$SCRIPT_DIR/results"
LOCUST_IMAGE="locust-runner"

USERS="${LOAD_TEST_USERS:-20}"
SPAWN_RATE="${LOAD_TEST_SPAWN_RATE:-2}"
RUN_TIME="${LOAD_TEST_RUN_TIME:-60s}"
HEALTH_TIMEOUT="${LOAD_TEST_HEALTH_TIMEOUT:-120}"

# ---------------------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[$(date +%H:%M:%S)]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

# ---------------------------------------------------------------------------

build_locust_runner() {
    log "Building locust runner image..."
    docker build -t "$LOCUST_IMAGE" "$SCRIPT_DIR"
    ok "Locust runner image built"
}

# ---------------------------------------------------------------------------
# Find docker compose file anywhere inside project dir
# ---------------------------------------------------------------------------
find_compose_file() {
    local dir="$1"

    [ -d "$dir" ] || {
        warn "  Directory not found: $dir"
        return 1
    }

    local found

    found="$(find "$dir" -maxdepth 4 \
        \( -name 'docker-compose.yml' -o -name 'docker-compose.yaml' \
           -o -name 'compose.yml' -o -name 'compose.yaml' \) \
        -not -path '*/.*' \
        2>/dev/null | head -n 1)" || true

    if [ -n "$found" ]; then
        echo "$found"
        return 0
    fi

    local f
    for f in \
        "$dir"/docker-compose.yml "$dir"/docker-compose.yaml \
        "$dir"/compose.yml "$dir"/compose.yaml \
        "$dir"/*/docker-compose.yml "$dir"/*/docker-compose.yaml \
        "$dir"/*/compose.yml "$dir"/*/compose.yaml \
        "$dir"/*/*/docker-compose.yml "$dir"/*/*/docker-compose.yaml \
        "$dir"/*/*/compose.yml "$dir"/*/*/compose.yaml \
        "$dir"/*/*/*/docker-compose.yml "$dir"/*/*/*/docker-compose.yaml \
        "$dir"/*/*/*/compose.yml "$dir"/*/*/*/compose.yaml; do
        [ -f "$f" ] && { echo "$f"; return 0; }
    done

    warn "  No compose file found in: $dir"
    warn "  Directory contents:"
    ls -la "$dir" 2>/dev/null | while IFS= read -r line; do warn "    $line"; done
    warn "  All YAML files (maxdepth 3):"
    find "$dir" -maxdepth 3 \( -name '*.yml' -o -name '*.yaml' \) \
        -not -path '*/.*' 2>/dev/null | while IFS= read -r line; do warn "    $line"; done

    return 1
}

# ---------------------------------------------------------------------------
# Find student's locustfile.py in project dir
# ---------------------------------------------------------------------------
find_locustfile() {
    local dir="$1"

    [ -d "$dir" ] || return 1

    local found

    found="$(find "$dir" -maxdepth 4 \
        -name 'locustfile.py' \
        -not -path '*/.*' \
        -not -path '*/venv/*' \
        -not -path '*/.venv/*' \
        -not -path '*/node_modules/*' \
        2>/dev/null | head -n 1)" || true

    if [ -n "$found" ]; then
        echo "$found"
        return 0
    fi

    return 1
}

# ---------------------------------------------------------------------------
# Wait for /health endpoint to respond 200
# ---------------------------------------------------------------------------
wait_for_health() {
    local start elapsed
    start="$(date +%s)"

    while true; do
        elapsed=$(( $(date +%s) - start ))
        if [ "$elapsed" -ge "$HEALTH_TIMEOUT" ]; then
            return 1
        fi

        if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
            return 0
        fi

        sleep 2
    done
}

# ---------------------------------------------------------------------------
# Parse Locust CSV stats and extract metrics into CSV row
# Format: name,status,total_requests,rps,http_errors,median_ms,avg_ms
# ---------------------------------------------------------------------------
extract_metrics() {
    local logdir="$1"
    local name="$2"

    local csv_file="$logdir/stats_stats.csv"

    if [ ! -f "$csv_file" ]; then
        echo "$name,TEST_INCOMPLETE,0,0,0,0,0"
        return
    fi

    local agg_line
    agg_line="$(grep 'Aggregated' "$csv_file" | tail -1)" || true

    if [ -z "$agg_line" ]; then
        echo "$name,TEST_INCOMPLETE,0,0,0,0,0"
        return
    fi

    local total errors median avg rps

    total="$(echo "$agg_line" | awk -F',' '{print $3}')"
    errors="$(echo "$agg_line" | awk -F',' '{print $4}')"
    median="$(echo "$agg_line" | awk -F',' '{print $5}')"
    avg="$(echo "$agg_line" | awk -F',' '{print $6}')"
    rps="$(echo "$agg_line" | awk -F',' '{print $10}')"

    total="${total:-0}"
    errors="${errors:-0}"
    median="${median:-0}"
    avg="${avg:-0}"
    rps="${rps:-0}"

    echo "$name,SUCCESS,$total,$rps,$errors,$median,$avg"
}

# ---------------------------------------------------------------------------
# Test a single project
# ---------------------------------------------------------------------------
test_project() {
    local name="$1"
    local project_dir="$2"
    local result_dir="$RESULTS_DIR/$name"

    mkdir -p "$result_dir"

    log "=========================================="
    log "Testing: $name"
    log "Project dir: $project_dir"
    log "=========================================="

    # --- 0. Find docker compose file ---
    local compose_file
    compose_file="$(find_compose_file "$project_dir" || true)"
    if [ -z "$compose_file" ]; then
        fail "$name: no docker-compose.yml / compose.yml found in $project_dir"
        echo "$name,NO_COMPOSE_FILE,0,0,0,0,0" >> "$RESULTS_DIR/summary.csv"
        return 1
    fi
    log "$name: compose $compose_file"

    # --- 1. Build and start ---
    log "Building and starting container..."
    if ! docker compose -f "$compose_file" up --build -d > "$result_dir/docker_build.log" 2>&1; then
        fail "$name: docker compose up failed (see $result_dir/docker_build.log)"
        return 1
    fi

    # --- 2. Wait for health ---
    log "Waiting for /health (timeout ${HEALTH_TIMEOUT}s)..."
    if ! wait_for_health; then
        fail "$name: /health did not respond within ${HEALTH_TIMEOUT}s"
        docker compose -f "$compose_file" logs >> "$result_dir/docker_build.log" 2>&1
        docker compose -f "$compose_file" down -v --remove-orphans > /dev/null 2>&1
        return 1
    fi
    ok "$name: service is healthy"

    # --- 3. Query /info ---
    local info_response
    info_response="$(curl -sf http://localhost:8000/info 2>/dev/null || echo '{}')"
    log "$name: /info -> $info_response"
    echo "$info_response" > "$result_dir/info.json"

    # --- 4. Find student's locustfile ---
    local student_locustfile
    student_locustfile="$(find_locustfile "$project_dir" || true)"

    local mount_locustfile=""
    if [ -n "$student_locustfile" ]; then
        log "$name: using student locustfile: $student_locustfile"
        mount_locustfile="-v $student_locustfile:/locust/locustfile.py:ro"
    else
        warn "$name: no locustfile.py found, using built-in fallback"
    fi

    # --- 5. Run locust ---
    log "Running load test (users=$USERS, spawn_rate=$SPAWN_RATE, time=$RUN_TIME)..."

    docker run --rm \
        --network host \
        -e LOCUST_HOST=http://localhost:8000 \
        -v "$result_dir":/locust/results \
        $mount_locustfile \
        "$LOCUST_IMAGE" \
        -f /locust/locustfile.py \
        --headless \
        --users "$USERS" \
        --spawn-rate "$SPAWN_RATE" \
        --run-time "$RUN_TIME" \
        --csv=/locust/results/stats \
        > "$result_dir/locust_stdout.log" 2>&1

    local locust_exit=$?
    if [ $locust_exit -ne 0 ]; then
        warn "$name: locust exited with code $locust_exit"
    else
        ok "$name: load test completed"
    fi

    # --- 6. Stop container ---
    log "Stopping container..."
    docker compose -f "$compose_file" down -v --remove-orphans > /dev/null 2>&1
    ok "$name: container stopped"
}

# ===========================================================================
# Main
# ===========================================================================

if [ ! -f "$PROJECTS_FILE" ]; then
    fail "projects.txt not found at $PROJECTS_FILE"
    exit 1
fi

mkdir -p "$RESULTS_DIR"

build_locust_runner

# Parse projects.txt, skip comments and empty lines
mapfile -t lines < <(grep -vE '^\s*(#|$)' "$PROJECTS_FILE")

if [ ${#lines[@]} -eq 0 ]; then
    fail "projects.txt is empty"
    exit 1
fi

log "Found ${#lines[@]} projects to test"
log "Settings: users=$USERS spawn_rate=$SPAWN_RATE run_time=$RUN_TIME"
echo ""

# Summary CSV header
echo "name,status,total_requests,rps,http_errors,median_ms,avg_ms" > "$RESULTS_DIR/summary.csv"

failed=0
total=${#lines[@]}
current=0

for line in "${lines[@]}"; do
    current=$((current + 1))

    name="$(echo "$line" | awk '{print $1}')"
    project_dir="$(echo "$line" | awk '{print $2}')"

    # Resolve relative paths relative to script dir
    if [[ "$project_dir" != /* ]]; then
        project_dir="$SCRIPT_DIR/$project_dir"
    fi

    if [ ! -d "$project_dir" ]; then
        fail "[$current/$total] $name: directory not found: $project_dir"
        echo "$name,DIR_NOT_FOUND,0,0,0,0,0" >> "$RESULTS_DIR/summary.csv"
        failed=$((failed + 1))
        continue
    fi

    log "[$current/$total] $name"

    if test_project "$name" "$project_dir"; then
        extract_metrics "$RESULTS_DIR/$name" "$name" >> "$RESULTS_DIR/summary.csv"
    else
        echo "$name,TEST_FAILED,0,0,0,0,0" >> "$RESULTS_DIR/summary.csv"
        failed=$((failed + 1))
    fi

    echo ""
done

# --- Final summary ---
log "=========================================="
log "ALL DONE"
log "=========================================="
log "Results: $RESULTS_DIR"
log "Summary: $RESULTS_DIR/summary.csv"
echo ""
echo "Name                | Status      | Requests | RPS   | Errors | Median | Avg"
echo "--------------------|-------------|----------|-------|--------|--------|------"

tail -n +2 "$RESULTS_DIR/summary.csv" | while IFS=, read -r name status total rps errors median avg; do
    printf "%-19s | %-11s | %8s | %5s | %6s | %4sms | %3sms\n" \
        "$name" "$status" "$total" "$rps" "$errors" "$median" "$avg"
done

echo ""
if [ $failed -eq 0 ]; then
    ok "All $total projects tested successfully"
else
    warn "$failed out of $total projects failed"
fi
