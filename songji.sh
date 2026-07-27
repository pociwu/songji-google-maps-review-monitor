#!/usr/bin/env bash

set -uo pipefail
export TZ=Asia/Taipei

PROJECT_ROOT="${PROJECT_ROOT:-/opt/maps-review-monitor}"
DB_FILE="${DB_FILE:-$PROJECT_ROOT/data/reviews.sqlite3}"
PYTHON_BIN="${PYTHON_BIN:-/home/ubuntu/miniconda3/envs/maps-review-monitor/bin/python}"
TIMER="${TIMER:-maps-review-monitor.timer}"
SERVICE="${SERVICE:-maps-review-monitor.service}"

if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || true)"
fi

show_reviews() {
    if [[ ! -f "$DB_FILE" ]]; then
        echo "錯誤：找不到資料庫：$DB_FILE" >&2
        return 1
    fi
    if [[ -z "$PYTHON_BIN" ]]; then
        echo "錯誤：找不到 Python 3" >&2
        return 1
    fi

    PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON_BIN" -m maps_review_monitor.review_browser "$DB_FILE"
}

format_remaining() {
    local target_epoch="$1"
    local now_epoch seconds days hours minutes

    if [[ -z "$target_epoch" ]]; then
        printf '無法計算'
        return
    fi

    now_epoch="$(date +%s)"
    seconds=$((target_epoch - now_epoch))
    if (( seconds <= 0 )); then
        printf '即將執行或已到期'
        return
    fi

    days=$((seconds / 86400))
    hours=$(((seconds % 86400) / 3600))
    minutes=$(((seconds % 3600) / 60))
    if (( days > 0 )); then
        printf '%d 天 %d 小時 %d 分鐘' "$days" "$hours" "$minutes"
    elif (( hours > 0 )); then
        printf '%d 小時 %d 分鐘' "$hours" "$minutes"
    else
        printf '%d 分鐘' "$minutes"
    fi
}

show_next_run() {
    local enabled timer_state service_state last_trigger next_trigger next_epoch
    local timer_dbus_path monotonic_property next_monotonic_us uptime_us remaining_us

    enabled="$(systemctl is-enabled "$TIMER" 2>/dev/null || true)"
    timer_state="$(systemctl is-active "$TIMER" 2>/dev/null || true)"
    service_state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
    last_trigger="$(systemctl show "$TIMER" --property=LastTriggerUSec --value 2>/dev/null || true)"
    next_trigger="$(systemctl show "$TIMER" --property=NextElapseUSecRealtime --value 2>/dev/null || true)"
    next_epoch=""

    if [[ -z "$next_trigger" || "$next_trigger" == "n/a" ]]; then
        timer_dbus_path="/org/freedesktop/systemd1/unit/maps_2dreview_2dmonitor_2etimer"
        monotonic_property="$(
            busctl get-property org.freedesktop.systemd1 "$timer_dbus_path" \
                org.freedesktop.systemd1.Timer NextElapseUSecMonotonic 2>/dev/null || true
        )"
        if [[ "$monotonic_property" =~ ^t[[:space:]]+([0-9]+)$ ]]; then
            next_monotonic_us="${BASH_REMATCH[1]}"
            uptime_us="$(awk '{printf "%.0f", $1 * 1000000}' /proc/uptime)"
            if (( next_monotonic_us > 0 && uptime_us > 0 )); then
                remaining_us=$((next_monotonic_us - uptime_us))
                (( remaining_us < 0 )) && remaining_us=0
                next_epoch=$(($(date +%s) + remaining_us / 1000000))
                next_trigger="$(date -d "@$next_epoch" '+%a %Y-%m-%d %H:%M:%S %Z')"
            fi
        fi
    fi

    [[ -n "$last_trigger" ]] || last_trigger="尚無紀錄"
    printf 'Google Maps 評論監控排程\n'
    printf '%s\n' '------------------------'
    printf '台灣時間：%s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    printf '開機啟用：%s\n' "${enabled:-unknown}"
    printf 'Timer 狀態：%s\n' "${timer_state:-unknown}"
    printf 'Service 狀態：%s\n' "${service_state:-unknown}"
    printf '上次觸發：%s\n' "$last_trigger"
    if [[ -n "$next_trigger" && "$next_trigger" != "n/a" ]]; then
        printf '下次執行：%s\n' "$next_trigger"
        printf '剩餘時間：%s\n' "$(format_remaining "$next_epoch")"
    else
        printf '下次執行：尚未排定\n'
    fi
}

pause_menu() {
    printf '\n按 Enter 返回選單...'
    read -r _
}

while true; do
    printf '\n'
    printf '%s\n' '=================================='
    printf '%s\n' ' Songji Google Maps 評論監控工具'
    printf '%s\n' '=================================='
    printf '%s\n' '1. 評論查詢'
    printf '%s\n' '2. 下次執行時間'
    printf '%s\n' '0. 離開'
    printf '%s' '請輸入選項 [0-2]：'

    if ! read -r choice; then
        printf '\n輸入已結束。\n'
        exit 0
    fi

    printf '\n'
    case "$choice" in
        1)
            show_reviews || true
            ;;
        2)
            show_next_run
            pause_menu
            ;;
        0|q|Q)
            echo '已離開。'
            exit 0
            ;;
        *)
            echo '輸入錯誤，請輸入 0、1 或 2。'
            pause_menu
            ;;
    esac
done
