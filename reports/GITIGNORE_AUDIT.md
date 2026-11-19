# AEON — .gitignore Audit & Rule Alignment Report

**Author:** Senior Quant Developer & Trading Systems Auditor  
**Date:** 2026-11-18  
**Scope:** Version control exclusion audit, secret protection, runtime state protection, and rule verification  
**Status:** COMPLETE  

---

## 1. Prior Weaknesses & Omissions

Prior to this audit, `.gitignore` exhibited several weaknesses:
1. **Blind Exclusion of `data/` Directory:**
   The previous `.gitignore` contained `data/` as a broad directory rule with only `!data/README.md`. Because `data/README.md` did not exist on disk, fresh clones had no `data/` folder, causing local scripts or unit tests to fail unless `mkdir -p data` was executed manually.
2. **Missing PyTest Temp Artifacts:**
   `.pytest_tmp/` and `.tmp_pytest/` were omitted, causing temporary test execution directories and ephemeral log files to show up as untracked files in `git status`.
3. **Missing Staging & Migration Artifacts:**
   `staging_backup_pre_patch/` and one-off patch scripts were untracked in Git status, creating visual noise and risk of accidental commit.
4. **Missing Editor & Cache Rules:**
   `.claude/` and partial temporary atomic write files (`*.partial.tmp`, `*.bad`) were not comprehensively matched.

---

## 2. Updated `.gitignore` Ruleset

The `.gitignore` has been updated and organized into clear, targeted sections:

```gitignore
# ==============================================================================
# AEON by Rekt — .gitignore
# ==============================================================================

# Python bytecode & caches
__pycache__/
*.py[cod]
*$py.class
*.so
.mypy_cache/
.tox/
.hypothesis/

# Test & coverage artifacts
.pytest_cache/
.pytest_tmp/
.tmp_pytest/
.coverage
.coverage.*
htmlcov/
*.log
telegram_outbox.log
bot.log

# Virtual environments
.venv/
venv/
env/
ENV/
env.bak/
venv.bak/

# Environment files & Secrets (never commit credentials)
.env
.env.*
.env.local
!.env.example

# IDE & Editor files
.vscode/
.idea/
*.swp
*.swo
*~
.claude/
.impeccable/

# OS-specific files
.DS_Store
Thumbs.db
desktop.ini

# Packaging & Build artifacts
dist/
build/
*.egg-info/
*.egg
*.tar.gz
*.zip

# Runtime state & data (preserve data directory structure, exclude live state)
data/*.json
data/*.json.bak
data/*.bad
data/*.tmp
data/backups/
!data/.gitkeep
!data/README.md

# Staging & manual backup folders
staging_backup_pre_patch/
backups-manuali/
data.before-restore-*/
test-extract/

# Temporary patch & migration artifacts
apply_aeon_patches.py
update_aeon_tests.py
scripts/apply_final_fixes.py
scripts/patch_dashboard_js.py
scripts/patch_dashboard_js_v2.py
scripts/tools_audit.py
```

---

## 3. Verification & Git Status Analysis

### Targeted Runtime State Protection
- Created `data/.gitkeep` and `data/README.md` to ensure `data/` is tracked on fresh clones.
- All live JSON state files (`state.json`, `heartbeat.json`, `brain.json`, `antirug_state.json`) and backups (`data/backups/`) are strictly ignored.
- Verified via `git status data/ -uall`:
  ```text
  Untracked files:
    data/.gitkeep
    data/README.md
  ```
  Zero live state or portfolio data is tracked.

### Template Tracking
- `.env.example` remains explicitly tracked (`!.env.example`), while any live `.env` or `.env.local` files containing secrets are ignored.

### Ephemeral Artifacts
- Running `pytest tests --basetemp="C:\Users\Ez__Rekt\Desktop\AOEN\.pytest_tmp"` creates no untracked files or warnings in `git status`.
- `staging_backup_pre_patch/` is completely ignored and protected from accidental commit.
