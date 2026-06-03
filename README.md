# Locust Runner

Load testing runner for student API projects. Docker-only, zero dependencies on the host machine.

## Setup on VM

```bash
# Clone alongside students directory
cd /home/user
git clone <this-repo-url>
```

Expected layout:
```
/home/user/
├── students/
│   ├── ivanov/
│   ├── petrov/
│   └── sidorov/
└── locust-runner/
    ├── Dockerfile
    ├── locustfile.py
    ├── run_all.sh
    └── projects.txt
```

## Usage

```bash
# 1. Fill projects.txt
echo "ivanov ../students/ivanov" >> projects.txt
echo "petrov ../students/petrov" >> projects.txt

# 2. Run
bash run_all.sh

# 3. Check results
cat results/summary.csv
```

## Configuration

All settings via environment variables:

```bash
LOAD_TEST_USERS=50 LOAD_TEST_SPAWN_RATE=5 LOAD_TEST_RUN_TIME=120s bash run_all.sh
```

| Variable | Default | Description |
|---|---|---|
| `LOAD_TEST_USERS` | 20 | Number of concurrent Locust users |
| `LOAD_TEST_SPAWN_RATE` | 2 | Users spawned per second |
| `LOAD_TEST_RUN_TIME` | 60s | Test duration per project |
| `LOAD_TEST_PROFILE` | constant | Load profile: constant, heavy, burst |
| `LOAD_TEST_HEALTH_TIMEOUT` | 120 | Seconds to wait for /health |

See `locustfile.py` header for additional `LOAD_TEST_*` variables (image size, text length, validation, etc.).

## Output

```
results/
├── ivanov/
│   ├── load_test_20260604T010000Z.log   # full structured log
│   ├── locust_stdout.log                 # Locust stats table
│   ├── info.json                         # /info response
│   └── docker_build.log                  # build logs (if failed)
├── petrov/
│   └── ...
└── summary.csv                           # CSV: name,status,requests,rps,failures,time
```

Final summary printed to console:
```
Name                | Status      | Requests | RPS   | Failures | Time
--------------------|-------------|----------|-------|----------|------
ivanov              | SUCCESS     |     1523 |  12.7 |        0 | 60.0s
petrov              | SUCCESS     |      847 |   7.1 |        2 | 60.0s
sidorov             | TEST_FAILED |        0 |     0 |        0 | 0s
```
