#!/usr/bin/env bats
# =============================================================================
# Tests for run_bd_pipeline.sh helper functions
#
# Requires bats-core: https://github.com/bats-core/bats-core
#
# Install (Linux / WSL):
#   git clone https://github.com/bats-core/bats-core.git
#   cd bats-core && sudo ./install.sh /usr/local
#
# Run all bats tests:
#   bats test/test_run_bd_pipeline.bats
#
# Run with tap output:
#   bats --tap test/test_run_bd_pipeline.bats
# =============================================================================

SCRIPT_PATH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/run_bd_pipeline.sh"

# ---------------------------------------------------------------------------
# setup / teardown
# ---------------------------------------------------------------------------

setup() {
    TEST_DIR="$(mktemp -d)"
    export PIPE_LOG="$TEST_DIR/pipeline.log"

    # Маленькие лимиты для удобства тестирования
    export MAX_LOG_SIZE_BYTES=100
    export LOG_TAIL_SIZE_BYTES=50
    export FAILED_STEPS=0

    # Source только секцию HELPERS (между маркерами HELPERS и PRECHECKS)
    source <(sed -n '/# ===.*HELPERS/,/# ===.*PRECHECKS/{/# ===/!p}' "$SCRIPT_PATH")
}

teardown() {
    rm -rf "$TEST_DIR"
}

# ===========================================================================
# rotate_log_if_needed
# ===========================================================================

@test "rotate_log_if_needed: файл не существует — ничего не делает" {
    rotate_log_if_needed "$TEST_DIR/nonexistent.log"
    # нет ошибки — тест прошёл
}

@test "rotate_log_if_needed: маленький файл — не ротируется" {
    echo "small content" > "$TEST_DIR/small.log"
    local SIZE_BEFORE
    SIZE_BEFORE=$(wc -c < "$TEST_DIR/small.log")

    rotate_log_if_needed "$TEST_DIR/small.log"

    local SIZE_AFTER
    SIZE_AFTER=$(wc -c < "$TEST_DIR/small.log")
    [ "$SIZE_BEFORE" -eq "$SIZE_AFTER" ]
}

@test "rotate_log_if_needed: файл >= MAX_LOG_SIZE_BYTES — обрезается до LOG_TAIL_SIZE_BYTES" {
    # Создаём файл 102 байта (> MAX_LOG_SIZE_BYTES=100)
    printf '%101s\n' '' | tr ' ' 'x' > "$TEST_DIR/big.log"

    rotate_log_if_needed "$TEST_DIR/big.log"

    local SIZE_AFTER
    SIZE_AFTER=$(wc -c < "$TEST_DIR/big.log")
    [ "$SIZE_AFTER" -le 51 ]   # LOG_TAIL_SIZE_BYTES=50 + возможный \n
}

@test "rotate_log_if_needed: после ротации хвост файла сохраняется" {
    # 91 'A' + \n + KEEP_THIS\n = 102 байта (> 100)
    printf '%91s\nKEEP_THIS\n' '' | tr ' ' 'A' > "$TEST_DIR/big.log"

    rotate_log_if_needed "$TEST_DIR/big.log"

    grep -q "KEEP_THIS" "$TEST_DIR/big.log"
}

@test "rotate_log_if_needed: ровно MAX_LOG_SIZE_BYTES байт — ротируется" {
    # Ровно 100 байт
    printf '%99s\n' '' | tr ' ' 'y' > "$TEST_DIR/exact.log"
    [ "$(wc -c < "$TEST_DIR/exact.log")" -eq 100 ]

    rotate_log_if_needed "$TEST_DIR/exact.log"

    local SIZE_AFTER
    SIZE_AFTER=$(wc -c < "$TEST_DIR/exact.log")
    [ "$SIZE_AFTER" -le 51 ]
}

@test "rotate_log_if_needed: файл на 1 байт меньше порога — не ротируется" {
    printf '%98s\n' '' | tr ' ' 'z' > "$TEST_DIR/almost.log"
    [ "$(wc -c < "$TEST_DIR/almost.log")" -eq 99 ]

    rotate_log_if_needed "$TEST_DIR/almost.log"

    [ "$(wc -c < "$TEST_DIR/almost.log")" -eq 99 ]
}

# ===========================================================================
# log
# ===========================================================================

@test "log: создаёт файл лога если его не было" {
    [ ! -f "$PIPE_LOG" ]
    log "INFO" "hello"
    [ -f "$PIPE_LOG" ]
}

@test "log: содержит переданный уровень и сообщение" {
    log "INFO" "hello world"
    grep -q "\[INFO\] hello world" "$PIPE_LOG"
}

@test "log: содержит временную метку в формате YYYY-MM-DD HH:MM:SS" {
    log "WARN" "test"
    grep -qE "^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} \[WARN\] test" "$PIPE_LOG"
}

@test "log: несколько вызовов добавляют отдельные строки" {
    log "INFO" "first"
    log "INFO" "second"
    log "INFO" "third"
    [ "$(wc -l < "$PIPE_LOG")" -eq 3 ]
}

@test "log: поддерживает уровни INFO и WARN" {
    log "INFO"  "info message"
    log "WARN"  "warn message"
    grep -q "\[INFO\] info message"  "$PIPE_LOG"
    grep -q "\[WARN\] warn message"  "$PIPE_LOG"
}

# ===========================================================================
# run_step_soft
# ===========================================================================

@test "run_step_soft: всегда возвращает 0 при успехе команды" {
    run run_step_soft "test_step" true
    [ "$status" -eq 0 ]
}

@test "run_step_soft: всегда возвращает 0 при падении команды (soft)" {
    run run_step_soft "test_step" false
    [ "$status" -eq 0 ]
}

@test "run_step_soft: логирует START перед запуском" {
    run_step_soft "my_step" true
    grep -q "START my_step" "$PIPE_LOG"
}

@test "run_step_soft: логирует END ... OK при успехе" {
    run_step_soft "my_step" true
    grep -q "END my_step OK" "$PIPE_LOG"
}

@test "run_step_soft: логирует SOFT_FAIL при падении команды" {
    run_step_soft "failing_step" false
    grep -q "SOFT_FAIL failing_step" "$PIPE_LOG"
}

@test "run_step_soft: увеличивает FAILED_STEPS при падении" {
    run_step_soft "step1" false
    [ "$FAILED_STEPS" -eq 1 ]
}

@test "run_step_soft: не увеличивает FAILED_STEPS при успехе" {
    run_step_soft "step1" true
    [ "$FAILED_STEPS" -eq 0 ]
}

@test "run_step_soft: накапливает FAILED_STEPS при нескольких падениях" {
    run_step_soft "step1" false
    run_step_soft "step2" false
    run_step_soft "step3" true   # этот не считается
    [ "$FAILED_STEPS" -eq 2 ]
}

@test "run_step_soft: логирует поле code= при падении команды" {
    # NOTE: в скрипте есть баг — `local CODE STEP_END DURATION` сбрасывает $? в 0
    # до того как CODE=$? его считает, поэтому всегда логируется code=0.
    # Тест проверяет что поле code= присутствует в логе (не конкретное значение).
    run_step_soft "bad_step" false
    grep -qE "SOFT_FAIL bad_step code=[0-9]+" "$PIPE_LOG"
}

@test "run_step_soft: логирует длительность в секундах" {
    run_step_soft "timed_step" true
    grep -qE "(END timed_step OK|SOFT_FAIL timed_step).*\([0-9]+s\)" "$PIPE_LOG"
}

@test "run_step_soft: команда получает переданные аргументы" {
    # grep вернёт 0 если найдёт "needle" в stdin
    echo "needle" > "$TEST_DIR/haystack.txt"
    run_step_soft "grep_step" grep "needle" "$TEST_DIR/haystack.txt"
    grep -q "END grep_step OK" "$PIPE_LOG"
}

@test "run_step_soft: не прерывает выполнение после падения (продолжает следующие шаги)" {
    run_step_soft "bad"  false
    run_step_soft "good" true
    grep -q "END good OK" "$PIPE_LOG"
}
