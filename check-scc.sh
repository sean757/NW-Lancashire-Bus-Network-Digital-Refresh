#!/usr/bin/env bash
# check-scc.sh - Health-check the SCC transport endpoints at transport.scc.lancs.ac.uk
#
# Checks performed per endpoint:
#   1. HTTP 404 / 403 / non-2xx, or an HTML response (text/html) -> POTENTIALLY DOWN
#   2. Empty body, or "empty" XML / JSON (e.g. <root/>, [], {}) -> LIKELY DOWN
#   3. Otherwise -> UP
#
# NOTE: You must be on campus or connected via the Lancaster University VPN
#       for these endpoints to be reachable.
#
# Usage: ./check-scc.sh [--base URL] [--timeout SECS] [--verbose]

set -u

BASE="http://transport.scc.lancs.ac.uk"
TIMEOUT=15
VERBOSE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base) BASE="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        -v|--verbose) VERBOSE=1; shift ;;
        -h|--help)
            sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: curl is required." >&2
    exit 1
fi

# ---- ANSI colours (only if stdout is a terminal) ----
if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'; C_GREEN=$'\033[32m'; C_RED=$'\033[31m'
    C_YELLOW=$'\033[33m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
else
    C_RESET=""; C_GREEN=""; C_RED=""; C_YELLOW=""; C_DIM=""; C_BOLD=""
fi

# ---- Operator codes (NOC) for bus endpoints ----
NOCS=(ARCT BLAC KLCO SCCU SCMY NUTT)

# ---- CRS codes for rail endpoints ----
CRS_CODES=(BPN LAY PFY BPS BPB SQU SAS AFV LTM MOS KKM SLW PRE LAN CNF SVR BAR MCM HHB LEY BMB)

# Results tallies
UP_COUNT=0; DOWN_COUNT=0; SUSPECT_COUNT=0
UP_LIST=(); DOWN_LIST=(); SUSPECT_LIST=()

# --------------------------------------------------------------------
# check_endpoint <path> [--allow-html]
#   Performs a ranged GET (first 8 KiB is enough to classify),
#   examines status code, content-type and body content.
#   Pass --allow-html for endpoints (e.g. directory indexes) where an
#   HTML response is expected and should NOT be treated as suspect.
# --------------------------------------------------------------------
check_endpoint() {
    local path="$1"
    local allow_html=0
    if [[ "${2:-}" == "--allow-html" ]]; then
        allow_html=1
    fi
    local url="${BASE}${path}"
    local tmp_body tmp_hdr
    tmp_body=$(mktemp); tmp_hdr=$(mktemp)

    # -s silent, -S show errors, -L follow redirects, -r range (first 8KiB),
    # -D dump-headers, -o body, -w status code, --max-time overall timeout.
    local status
    status=$(curl -sS -L --max-time "$TIMEOUT" \
        -r 0-8191 \
        -H 'Accept: application/xml, application/json, text/xml, text/plain, */*' \
        -D "$tmp_hdr" -o "$tmp_body" \
        -w '%{http_code}' "$url" 2>/dev/null) || status="000"

    local ctype
    ctype=$(awk 'BEGIN{IGNORECASE=1} /^content-type:/ {sub(/^[^:]*: */,""); print; exit}' "$tmp_hdr" \
            | tr -d '\r\n' | tr '[:upper:]' '[:lower:]')

    local size
    size=$(wc -c < "$tmp_body" | tr -d ' ')

    # Trim body of leading/trailing whitespace for emptiness checks
    local body_trimmed
    body_trimmed=$(tr -d '[:space:]' < "$tmp_body")

    local verdict="UP"
    local reason=""

    # ---- Check 1: bad status or HTML response ----
    if [[ "$status" == "000" ]]; then
        verdict="SUSPECT"
        reason="no response / connection failure"
    elif [[ "$status" == "404" || "$status" == "403" ]]; then
        verdict="SUSPECT"
        reason="HTTP $status"
    elif [[ "$status" =~ ^(4|5)[0-9][0-9]$ && "$status" != "416" && "$status" != "206" ]]; then
        # 416 = range not satisfiable (tiny file); 206 = partial content (expected)
        verdict="SUSPECT"
        reason="HTTP $status"
    elif [[ "$ctype" == *"text/html"* ]]; then
        if [[ $allow_html -eq 1 ]]; then
            :   # HTML expected here (e.g. directory index) - not suspect
        else
            verdict="SUSPECT"
            reason="HTML response (content-type: $ctype)"
        fi
    elif head -c 512 "$tmp_body" | grep -qiE '<!doctype html|<html[ >]'; then
        if [[ $allow_html -eq 1 ]]; then
            :
        else
            verdict="SUSPECT"
            reason="HTML body detected"
        fi
    fi

    # ---- Check 2: empty body / empty XML / empty JSON ----
    if [[ "$verdict" == "UP" ]]; then
        if [[ -z "$body_trimmed" || "$size" -eq 0 ]]; then
            verdict="DOWN"
            reason="empty body"
        elif [[ "$body_trimmed" == "{}" || "$body_trimmed" == "[]" || "$body_trimmed" == "null" ]]; then
            verdict="DOWN"
            reason="empty JSON ($body_trimmed)"
        elif [[ "$body_trimmed" =~ ^(\<\?xml[^?]*\?\>)?\<[A-Za-z_][A-Za-z0-9_:-]*/\>$ ]]; then
            verdict="DOWN"
            reason="empty XML document"
        elif [[ "$body_trimmed" =~ ^(\<\?xml[^?]*\?\>)?\<([A-Za-z_][A-Za-z0-9_:-]*)[^\>]*\>\</\2\>$ ]]; then
            verdict="DOWN"
            reason="empty XML document"
        fi
    fi

    # ---- Report ----
    local tag
    case "$verdict" in
        UP)      tag="${C_GREEN}[ UP      ]${C_RESET}"; UP_COUNT=$((UP_COUNT+1))
                 UP_LIST+=("$path") ;;
        DOWN)    tag="${C_RED}[ DOWN    ]${C_RESET}";   DOWN_COUNT=$((DOWN_COUNT+1))
                 DOWN_LIST+=("$path ($reason)") ;;
        SUSPECT) tag="${C_YELLOW}[ SUSPECT ]${C_RESET}"; SUSPECT_COUNT=$((SUSPECT_COUNT+1))
                 SUSPECT_LIST+=("$path ($reason)") ;;
    esac

    printf '  %s %-40s status=%s size=%s%s\n' \
        "$tag" "$path" "$status" "$size" \
        "${reason:+  ${C_DIM}- ${reason}${C_RESET}}"

    if [[ $VERBOSE -eq 1 ]]; then
        printf '      %scontent-type: %s%s\n' "$C_DIM" "${ctype:-<none>}" "$C_RESET"
        printf '      %ssnippet: %s%s\n' "$C_DIM" \
            "$(head -c 120 "$tmp_body" | tr '\n\r\t' '   ' | sed 's/  */ /g')" "$C_RESET"
    fi

    rm -f "$tmp_body" "$tmp_hdr"
}

section() {
    printf '\n%s== %s ==%s\n' "$C_BOLD" "$1" "$C_RESET"
}

echo "Target base URL : $BASE"
echo "Per-request timeout: ${TIMEOUT}s"
echo "Started: $(date)"

# -------------------- NPTG / NaPTAN --------------------
section "National Public Transport Gazetteer / NaPTAN"
check_endpoint "/nptg/nptg.xml"
check_endpoint "/nptg/naptan.xml"
check_endpoint "/nptg/naptan-full.xml"

# -------------------- National Highways ----------------
section "National Highways"
check_endpoint "/road/vms"

# -------------------- Weather --------------------------
section "Weather"
check_endpoint "/weather?lat=54.05&lon=-2.80"
check_endpoint "/weather/icons/04n"

# -------------------- Buses (per NOC) ------------------
section "Buses - /bus/times/<NOC>"
for noc in "${NOCS[@]}"; do
    check_endpoint "/bus/times/${noc}"
done

section "Buses - /bus/live/<NOC>"
for noc in "${NOCS[@]}"; do
    check_endpoint "/bus/live/${noc}"
done

# -------------------- Rail: departures / facilities ----
section "Rail - /rail/departures/<CRS>"
for crs in "${CRS_CODES[@]}"; do
    check_endpoint "/rail/departures/${crs}"
done

section "Rail - /rail/facilities/<CRS>"
for crs in "${CRS_CODES[@]}"; do
    check_endpoint "/rail/facilities/${crs}"
done

# -------------------- Rail: static datasets ------------
section "Rail - static datasets"
check_endpoint "/rail/bplan.txt"
check_endpoint "/rail/corpus"
check_endpoint "/rail/smart"
check_endpoint "/rail/delay-codes.json"
check_endpoint "/rail/schedule"
check_endpoint "/rail/timetable/"          --allow-html
check_endpoint "/rail/track-model/"        --allow-html

# -------------------- Summary --------------------------
section "Summary"
printf '  %sUP      :%s %d\n' "$C_GREEN" "$C_RESET" "$UP_COUNT"
printf '  %sDOWN    :%s %d  (empty body / empty XML or JSON)\n' "$C_RED"    "$C_RESET" "$DOWN_COUNT"
printf '  %sSUSPECT :%s %d  (404/403, HTML, or connection failure)\n' "$C_YELLOW" "$C_RESET" "$SUSPECT_COUNT"

if [[ ${#DOWN_LIST[@]} -gt 0 ]]; then
    printf '\n%sLikely DOWN:%s\n' "$C_RED" "$C_RESET"
    for item in "${DOWN_LIST[@]}"; do printf '  - %s\n' "$item"; done
fi
if [[ ${#SUSPECT_LIST[@]} -gt 0 ]]; then
    printf '\n%sPotentially DOWN:%s\n' "$C_YELLOW" "$C_RESET"
    for item in "${SUSPECT_LIST[@]}"; do printf '  - %s\n' "$item"; done
fi

echo
echo "Finished: $(date)"

# Exit code: 0 if everything is UP, 1 if any DOWN/SUSPECT found.
if [[ $DOWN_COUNT -gt 0 || $SUSPECT_COUNT -gt 0 ]]; then
    exit 1
fi
exit 0
