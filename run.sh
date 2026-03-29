#!/usr/bin/env bash
# =============================================================================
# LCI Claims Automation — One-Command Runner
# =============================================================================
# Usage:
#   ./run.sh                  # Full daily run (dry-run mode by default)
#   ./run.sh --live           # Full daily run in LIVE mode (actually submits)
#   ./run.sh --live --visible # Live mode with visible browser (not headless)
#   ./run.sh --action era     # Only run ERA upload
#   ./run.sh --schedule       # Start daily scheduler (runs Mon-Fri at 7am)
#   ./run.sh --setup          # First-time setup only (install deps + browser)
#   ./run.sh --test           # Run test suite
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC}  $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }

# ── Parse arguments ──────────────────────────────────────────────────────
LIVE=false
SETUP_ONLY=false
TEST_ONLY=false
EXTRA_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --live)    LIVE=true ;;
        --setup)   SETUP_ONLY=true ;;
        --test)    TEST_ONLY=true ;;
        *)         EXTRA_ARGS+=("$arg") ;;
    esac
done

# ── Ensure virtual environment exists ────────────────────────────────────
if [ ! -d ".venv" ]; then
    echo -e "${YELLOW}▶ First-time setup: creating virtual environment...${NC}"

    # Check Python version
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
    if [[ $MAJOR -lt 3 || ($MAJOR -eq 3 && $MINOR -lt 11) ]]; then
        fail "Python 3.11+ required (found $PYTHON_VERSION)"
    fi
    ok "Python $PYTHON_VERSION"

    python3 -m venv .venv
    ok "Virtual environment created"
fi

# ── Activate venv ────────────────────────────────────────────────────────
source .venv/bin/activate

# ── Install dependencies if needed ───────────────────────────────────────
if [ ! -f ".venv/.deps_installed" ] || [ "requirements.txt" -nt ".venv/.deps_installed" ]; then
    echo -e "${YELLOW}▶ Installing dependencies...${NC}"
    pip install -r requirements.txt -q
    pip install pytest pytest-asyncio pytest-mock -q 2>/dev/null || true
    ok "Dependencies installed"

    # Install Playwright Chromium
    echo -e "${YELLOW}▶ Installing Chromium browser...${NC}"
    python3 -m playwright install chromium 2>/dev/null
    ok "Chromium installed"

    touch .venv/.deps_installed
fi

# ── Create required directories ──────────────────────────────────────────
mkdir -p logs sessions data

# ── Check .env exists ────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    fail ".env file not found! Copy .env.template to .env and fill in credentials."
fi

# ── Setup-only mode ──────────────────────────────────────────────────────
if [ "$SETUP_ONLY" = true ]; then
    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Setup complete! Ready to run.                     ${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
    echo ""
    echo "  Dry run:    ./run.sh"
    echo "  Live run:   ./run.sh --live"
    echo "  Run tests:  ./run.sh --test"
    echo ""
    exit 0
fi

# ── Test mode ────────────────────────────────────────────────────────────
if [ "$TEST_ONLY" = true ]; then
    echo -e "${YELLOW}▶ Running test suite...${NC}"
    python3 -m pytest tests/ -v --tb=short
    exit $?
fi

# ── Validate imports ─────────────────────────────────────────────────────
python3 -c "
from config.models import Claim, MCO, DenialCode, ResolutionAction
from config.settings import DRY_RUN, get_credentials
from notes.formatter import format_note
from decision_tree.router import ClaimRouter
" 2>/dev/null || fail "Import validation failed — check Python path"

# ── Set DRY_RUN based on --live flag ─────────────────────────────────────
if [ "$LIVE" = true ]; then
    export DRY_RUN=false
    warn "LIVE MODE — automation will make real changes to portals and Claim.MD"
    # Only ask for confirmation if running interactively (not from scheduler)
    if [ -t 0 ]; then
        echo ""
        read -p "Are you sure you want to run in LIVE mode? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Cancelled."
            exit 0
        fi
    fi
else
    export DRY_RUN=true
    ok "DRY RUN MODE — no real changes will be made"
fi

# ── Run the automation ───────────────────────────────────────────────────
echo ""
echo -e "${GREEN}▶ Starting LCI Claims Automation...${NC}"
echo ""

if [ "$LIVE" = false ]; then
    python3 orchestrator.py --dry-run "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
else
    python3 orchestrator.py "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
fi

echo ""
ok "Run complete. Check logs/ for details."
echo "  Logs:          logs/claims_$(date +%Y-%m-%d).jsonl"
echo "  Human review:  logs/human_review_$(date +%Y-%m-%d).json"
echo ""
