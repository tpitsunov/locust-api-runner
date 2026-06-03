#!/usr/bin/env bash
set -euo pipefail

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
#   │   ├── load_test_<ts>.log    — full locustfile log
#   │   ├── locust_stdout.log     — Locust stdout (stats table)
#   │   ├── info.json             — /info response
#   │   └── docker_build.log      — build logs (on failure)
#   ├── petrov/
#   │   └── ...
#   └── summary.csv               — summary across all projects
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECTS_FILE="$SCRIPT_DIR/projects.txt"
RESULTS_DIR="$SCRIPT_DIR/results"
LOCUST_IMAGE="locust-runner"

USERS="${LOAD_TEST_USERS:-20}"
SPAWN_RATE="${LOAD_TEST_SPAWN_RATE:-2}"
RUN_TIME="${LOAD_TEST_RUN_TIME:-60s}"
PROFILE="${LOAD_TEST_PROFILE:-constant}"
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
    if docker image inspect "$LOCUST_IMAGE" > /dev/null 2>&1; then
        log "Locust runner image already exists, skipping build"
        return 0
    fi

    log "Building locust runner image..."
    docker build -t "$LOCUST_IMAGE" "$SCRIPT_DIR"
    ok "Locust runner image built"
}

# ---------------------------------------------------------------------------
# Parse locust log file and extract metrics into CSV row
# Format: name,status,total_requests,rps,validation_failures,elapsed_s
# ---------------------------------------------------------------------------
extract_metrics() {
    local logdir="$1"
    local name="$2"

    local log_file
    log_file="$(ls -t "$logdir"/load_test_*.log 2>/dev/null | head -1)"

    if [ -z "$log_file" ]; then
        echo "$name,BUILD_FAILED,0,0,0,0"
        return
    fi

    if grep -q "LOAD TEST FINISHED" "$log_file"; then
        local total rps failures elapsed

        total="$(grep 'Total requests' "$log_file" | grep -oE '[0-9]+' | head -1)"
        rps="$(grep 'Requests/sec' "$log_file" | grep -oE '[0-9]+\.[0-9]+' | head -1)"
        failures="$(grep 'Validation failures' "$log_file" | grep -oE '[0-9]+' | head -1)"
        elapsed="$(grep 'Elapsed' "$log_file" | grep -oE '[0-9]+\.[0-9]+' | head -1)"

        total="${total:-0}"
        rps="${rps:-0}"
        failures="${failures:-0}"
        elapsed="${elapsed:-0}"

        echo "$name,SUCCESS,$total,$rps,$failures,$elapsed"
    else
        echo "$name,TEST_INCOMPLETE,0,0,0,0"
    fi
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

    # --- 1. Build and start ---
    log "Building and starting container..."
    if ! docker compose -f "$project_dir/docker-compose.yml" up --build -d > "$result_dir/docker_build.log" 2>&1; then
        fail "$name: docker compose up failed (see $result_dir/docker_build.log)"
        return 1
    fi

    # --- 2. Wait for health ---
    log "Waiting for /health (timeout ${HEALTH_TIMEOUT}s)..."
    if ! wait_for_health; then
        fail "$name: /health did not respond within ${HEALTH_TIMEOUT}s"
        docker compose -f "$project_dir/docker-compose.yml" logs >> "$result_dir/docker_build.log" 2>&1
        docker compose -f "$project_dir/docker-compose.yml" down -v --remove-orphans > /dev/null 2>&1
        return 1
    fi
    ok "$name: service is healthy"

    # --- 3. Query /info ---
    local info_response
    info_response="$(curl -sf http://localhost:8000/info 2>/dev/null || echo '{}')"
    log "$name: /info -> $info_response"
    echo "$info_response" > "$result_dir/info.json"

    # --- 4. Run locust ---
    log "Running load test (users=$USERS, spawn_rate=$SPAWN_RATE, time=$RUN_TIME, profile=$PROFILE)..."

    docker run --rm \
        --network host \
        -e LOCUST_HOST=http://localhost:8000 \
        -e LOAD_TEST_PROFILE="$PROFILE" \
        -e LOAD_TEST_VALIDATE=1 \
        -e LOAD_TEST_WARMUP_REQUESTS=3 \
        -e LOAD_TEST_LOG_DIR=/locust/logs \
        -v "$result_dir":/locust/logs \
        "$LOCUST_IMAGE" \
        --headless \
        --users "$USERS" \
        --spawn-rate "$SPAWN_RATE" \
        --run-time "$RUN_TIME" \
        > "$result_dir/locust_stdout.log" 2>&1

    local locust_exit=$?
    if [ $locust_exit -ne 0 ]; then
        warn "$name: locust exited with code $locust_exit"
    else
        ok "$name: load test completed"
    fi

    # --- 5. Stop container ---
    log "Stopping container..."
    docker compose -f "$project_dir/docker-compose.yml" down -v --remove-orphans > /dev/null 2>&1
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
log "Settings: users=$USERS spawn_rate=$SPAWN_RATE run_time=$RUN_TIME profile=$PROFILE"
echo ""

# Summary CSV header
echo "name,status,total_requests,rps,validation_failures,elapsed_s" > "$RESULTS_DIR/summary.csv"

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
        echo "$name,DIR_NOT_FOUND,0,0,0,0" >> "$RESULTS_DIR/summary.csv"
        failed=$((failed + 1))
        continue
    fi

    log "[$current/$total] $name"

    if test_project "$name" "$project_dir"; then
        extract_metrics "$RESULTS_DIR/$name" "$name" >> "$RESULTS_DIR/summary.csv"
    else
        echo "$name,TEST_FAILED,0,0,0,0" >> "$RESULTS_DIR/summary.csv"
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
echo "Name                | Status      | Requests | RPS   | Failures | Time"
echo "--------------------|-------------|----------|-------|----------|------"

tail -n +2 "$RESULTS_DIR/summary.csv" | while IFS=, read -r name status total rps failures elapsed; do
    printf "%-19s | %-11s | %8s | %5s | %8s | %ss\n" \
        "$name" "$status" "$total" "$rps" "$failures" "$elapsed"
done

echo ""
if [ $failed -eq 0 ]; then
    ok "All $total projects tested successfully"
else
    warn "$failed out of $total projects failed"
fi
