#!/usr/bin/env bash
# =============================================================================
# install.sh — LCI Claims Automation Setup
# =============================================================================
# Usage:
#   chmod +x install.sh && ./install.sh
#   ./install.sh --skip-browser   # Skip Playwright install (for CI)
#   ./install.sh --dev            # Include dev dependencies
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${LOG_DIR:-/var/log/claims_automation}"
SESSION_DIR="${SESSION_DIR:-/tmp/claims_sessions}"
WORK_DIR="/tmp/claims_work"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC}  $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
step() { echo -e "\n${YELLOW}▶ $1${NC}"; }

SKIP_BROWSER=false
DEV_DEPS=false
for arg in "$@"; do
    [[ "$arg" == "--skip-browser" ]] && SKIP_BROWSER=true
    [[ "$arg" == "--dev"          ]] && DEV_DEPS=true
done

# ── Python version check ──────────────────────────────────────────────────
step "Checking Python version"
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
if [[ $MAJOR -lt 3 || ($MAJOR -eq 3 && $MINOR -lt 11) ]]; then
    fail "Python 3.11+ required (found $PYTHON_VERSION)"
fi
ok "Python $PYTHON_VERSION"

# ── Create directories ────────────────────────────────────────────────────
step "Creating directories"
mkdir -p "$LOG_DIR" "$SESSION_DIR" "$WORK_DIR/eras" "$WORK_DIR/fax"
ok "Log:     $LOG_DIR"
ok "Session: $SESSION_DIR"
ok "Work:    $WORK_DIR"

# ── Install Python dependencies ───────────────────────────────────────────
step "Installing Python dependencies"
cd "$SCRIPT_DIR"
pip install -r requirements.txt -q
ok "Core dependencies installed"

if [[ "$DEV_DEPS" == "true" ]]; then
    pip install pytest pytest-asyncio pytest-mock -q
    ok "Dev dependencies installed"
fi

# ── Install python-docx (for Word cover letters) ─────────────────────────
step "Checking python-docx"
python3 -c "from docx import Document" 2>/dev/null && ok "python-docx present" || {
    pip install python-docx -q
    ok "python-docx installed"
}

# ── Playwright browser ────────────────────────────────────────────────────
if [[ "$SKIP_BROWSER" == "false" ]]; then
    step "Installing Playwright Chromium"
    python3 -m playwright install chromium
    python3 -m playwright install-deps chromium 2>/dev/null || true
    ok "Chromium installed"
else
    warn "Skipping Playwright install (--skip-browser)"
fi

# ── LibreOffice (optional, for PDF conversion) ───────────────────────────
step "Checking LibreOffice"
if command -v libreoffice &>/dev/null || command -v soffice &>/dev/null; then
    ok "LibreOffice present (PDF conversion available)"
else
    warn "LibreOffice not found — cover letter PDF conversion will use fallback"
    warn "Install with: sudo apt-get install -y libreoffice"
fi

# ── Environment file ──────────────────────────────────────────────────────
step "Checking .env"
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    ok ".env present"
else
    cp "$SCRIPT_DIR/.env.template" "$SCRIPT_DIR/.env"
    warn ".env created from template — FILL IN CREDENTIALS before running!"
    warn "Required: CLAIMMD_USERNAME, CLAIMMD_PASSWORD, LAURIS_URL,"
    warn "          LAURIS_USERNAME, LAURIS_PASSWORD, CLICKUP_API_TOKEN"
fi

# ── Validate imports ──────────────────────────────────────────────────────
step "Validating Python imports"
python3 -c "
from config.models import Claim, MCO, DenialCode, ResolutionAction
from config.settings import DRY_RUN
from notes.formatter import format_note
from decision_tree.router import ClaimRouter
from sources.claimmd import parse_denial_codes
from lauris.billing import classify_era
from actions.fax_refax import build_refax_cover_letter
print('All imports OK')
" && ok "All modules importable" || fail "Import error — check PYTHONPATH"

# ── Run tests ─────────────────────────────────────────────────────────────
step "Running test suite"
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -5
ok "Tests complete"

# ── Print summary ─────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  LCI Claims Automation — Installation Complete!    ${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo ""
echo "Next steps:"
echo ""
echo "  1. Edit .env and fill in all credentials"
echo "     nano $SCRIPT_DIR/.env"
echo ""
echo "  2. Test dry run (logs only, no submissions):"
echo "     python $SCRIPT_DIR/orchestrator.py --dry-run"
echo ""
echo "  3. Audit portal selectors (checks that portal HTML hasn't changed):"
echo "     python $SCRIPT_DIR/tools/selector_audit.py --portal claimmd"
echo ""
echo "  4. Run for real:"
echo "     python $SCRIPT_DIR/orchestrator.py"
echo ""
echo "  5. Schedule daily (Mon-Fri 7am):"
echo "     python $SCRIPT_DIR/orchestrator.py --schedule"
echo ""
echo "  Logs → $LOG_DIR"
echo "  Human review queue → $LOG_DIR/human_review_YYYY-MM-DD.json"
echo ""
