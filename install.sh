#!/usr/bin/env bash
# ==============================================================================
# AEON by Rekt — VPS Installation Script with Safe Rollback
# Ubuntu 24.04 LTS | Docker + systemd hybrid | Idempotent
# ==============================================================================
# Usage: sudo bash install.sh [--dry-run] [--rollback] [--no-docker] [--domain example.com] [--email admin@example.com]
# ==============================================================================

set -euo pipefail

# ------------------------------------------------------------------------------
# Configuration & Constants
# ------------------------------------------------------------------------------
readonly SCRIPT_VERSION="1.1.0"
readonly PROJECT_NAME="aeon-by-rekt"
readonly INSTALL_DIR="/opt/${PROJECT_NAME}"
readonly SERVICE_USER="aeon"
readonly SERVICE_GROUP="aeon"
readonly DATA_DIR="${INSTALL_DIR}/data"
readonly BACKUP_DIR="${INSTALL_DIR}/backups-manuali"
readonly LOG_DIR="/var/log/${PROJECT_NAME}"

# Colors
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly CYAN='\033[0;36m'
readonly NC='\033[0m' # No Color

# Flags
DRY_RUN=false
FORCE_DOCKER=true
DOMAIN=""
EMAIL=""
ROLLBACK_MODE=false
ROLLBACK_FILE=""

# Rollback tracking
TIMESTAMP=$(date +%s)
readonly ROLLBACK_TRACKER="/tmp/aeon-install-rollback-${TIMESTAMP}.json"

# Global state tracking
declare -A TRACKED_FILES
declare -A TRACKED_DIRS
declare -A TRACKED_USERS
declare -A TRACKED_SYSTEMD_SERVICES
declare -A TRACKED_DOCKER_IMAGES
declare -A TRACKED_DOCKER_CONTAINERS
declare -A TRACKED_CRON_JOBS
declare -A TRACKED_UFW_RULES
declare -A TRACKED_FILE_BACKUPS
declare -A PRE_EXISTING_DOCKER=false
declare -A PRE_EXISTING_N8N=false
declare -A PRE_EXISTING_CADDY=false

# ------------------------------------------------------------------------------
# Rollback Tracker Functions
# ------------------------------------------------------------------------------

init_rollback_tracker() {
    cat > "${ROLLBACK_TRACKER}" <<EOF
{
  "timestamp": ${TIMESTAMP},
  "script_version": "${SCRIPT_VERSION}",
  "install_dir": "${INSTALL_DIR}",
  "service_user": "${SERVICE_USER}",
  "tracked": {
    "files_created": [],
    "dirs_created": [],
    "users_created": [],
    "systemd_services_installed": [],
    "docker_images_pulled": [],
    "docker_images_built": [],
    "docker_containers_created": [],
    "cron_jobs_added": [],
    "ufw_rules_added": [],
    "files_modified": []
  },
  "pre_existing": {
    "docker_installed": false,
    "n8n_container": false,
    "caddy_container": false,
    "caddy_service": false
  },
  "phases_completed": []
}
EOF
    debug "Rollback tracker initialized: ${ROLLBACK_TRACKER}"
}

update_rollback_tracker() {
    local key="$1"
    local value="$2"
    local tracker_content=$(cat "${ROLLBACK_TRACKER}")

    # Use jq if available, otherwise use sed
    if command -v jq &> /dev/null; then
        echo "${tracker_content}" | jq --arg key "${key}" --argjson value "${value}" '.tracked[$key] = $value' > "${ROLLBACK_TRACKER}.tmp" && mv "${ROLLBACK_TRACKER}.tmp" "${ROLLBACK_TRACKER}"
    else
        # Fallback: basic JSON manipulation without jq
        debug "jq not available, skipping tracker update for ${key}"
    fi
}

add_to_tracker() {
    local category="$1"
    local item="$2"
    local tracker_content=$(cat "${ROLLBACK_TRACKER}")

    if command -v jq &> /dev/null; then
        echo "${tracker_content}" | jq --arg cat "${category}" --arg item "${item}" '.tracked[$cat] += [$item] | unique' > "${ROLLBACK_TRACKER}.tmp" && mv "${ROLLBACK_TRACKER}.tmp" "${ROLLBACK_TRACKER}"
    else
        debug "jq not available, skipping tracker add for ${category}: ${item}"
    fi
}

add_file_backup() {
    local original_file="$1"
    local backup_file="$2"
    local tracker_content=$(cat "${ROLLBACK_TRACKER}")

    if command -v jq &> /dev/null; then
        echo "${tracker_content}" | jq --arg orig "${original_file}" --arg backup "${backup_file}" '.tracked.files_modified += [{"original": $orig, "backup": $backup}] | unique' > "${ROLLBACK_TRACKER}.tmp" && mv "${ROLLBACK_TRACKER}.tmp" "${ROLLBACK_TRACKER}"
    else
        debug "jq not available, skipping file backup tracking"
    fi
}

mark_phase_completed() {
    local phase="$1"
    local tracker_content=$(cat "${ROLLBACK_TRACKER}")

    if command -v jq &> /dev/null; then
        echo "${tracker_content}" | jq --arg phase "${phase}" '.phases_completed += [$phase] | unique' > "${ROLLBACK_TRACKER}.tmp" && mv "${ROLLBACK_TRACKER}.tmp" "${ROLLBACK_TRACKER}"
    fi
}

set_pre_existing() {
    local key="$1"
    local value="$2"
    local tracker_content=$(cat "${ROLLBACK_TRACKER}")

    if command -v jq &> /dev/null; then
        echo "${tracker_content}" | jq --arg key "${key}" --argjson value "${value}" '.pre_existing[$key] = $value' > "${ROLLBACK_TRACKER}.tmp" && mv "${ROLLBACK_TRACKER}.tmp" "${ROLLBACK_TRACKER}"
    fi
}

# ------------------------------------------------------------------------------
# Utility Functions
# ------------------------------------------------------------------------------
log() { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
debug() { [[ "${DEBUG:-}" == "1" ]] && echo -e "${CYAN}[DEBUG]${NC} $*"; }
step() { echo -e "\n${BLUE}==>${NC} ${CYAN}$*${NC}"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }

usage() {
    cat <<EOF
Usage: sudo bash install.sh [OPTIONS]

Options:
  --dry-run          Simulate installation without making changes
  --rollback FILE    Rollback using the specified rollback tracker file
  --no-docker        Use systemd instead of Docker (not recommended)
  --domain DOMAIN    Configure Caddy with domain (e.g., bot.example.com)
  --email EMAIL      Email for Let's Encrypt (required with --domain)
  -h, --help         Show this help

Examples:
  sudo bash install.sh
  sudo bash install.sh --domain bot.example.com --email admin@example.com
  sudo bash install.sh --dry-run
  sudo bash install.sh --rollback /tmp/aeon-install-rollback-1234567890.json

EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --dry-run) DRY_RUN=true; shift ;;
            --rollback) ROLLBACK_MODE=true; ROLLBACK_FILE="${2:-}"; shift 2 ;;
            --no-docker) FORCE_DOCKER=false; shift ;;
            --domain) DOMAIN="${2:-}"; shift 2 ;;
            --email) EMAIL="${2:-}"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) error "Unknown option: $1"; usage; exit 1 ;;
        esac
    done

    if [[ "$ROLLBACK_MODE" == "true" ]]; then
        if [[ -z "$ROLLBACK_FILE" || ! -f "$ROLLBACK_FILE" ]]; then
            error "Rollback file not found: $ROLLBACK_FILE"
            exit 1
        fi
        return 0
    fi

    if [[ -n "$DOMAIN" && -z "$EMAIL" ]]; then
        error "--domain requires --email"
        exit 1
    fi
}

run() {
    if [[ "$DRY_RUN" == "true" ]]; then
        debug "DRY-RUN: $*"
    else
        eval "$@"
    fi
}

# ------------------------------------------------------------------------------
# Rollback Functions
# ------------------------------------------------------------------------------

perform_rollback() {
    step "Starting rollback using: ${ROLLBACK_FILE}"

    if [[ ! -f "$ROLLBACK_FILE" ]]; then
        error "Rollback tracker not found: $ROLLBACK_FILE"
        exit 1
    fi

    if ! command -v jq &> /dev/null; then
        error "jq is required for rollback but not installed. Install with: apt-get install -y jq"
        exit 1
    fi

    local tracker_content=$(cat "$ROLLBACK_FILE")
    local install_dir=$(echo "$tracker_content" | jq -r '.install_dir')
    local service_user=$(echo "$tracker_content" | jq -r '.service_user')

    log "Rollback for installation at: $install_dir"
    log "Service user: $service_user"

    # Rollback in reverse order

    # 1. Stop and remove Docker containers (only ones we created)
    rollback_docker_containers "$tracker_content"

    # 2. Remove Docker images we built (not pulled)
    rollback_docker_images_built "$tracker_content"

    # 3. Remove cron jobs
    rollback_cron_jobs "$tracker_content"

    # 4. Remove UFW rules we added
    rollback_ufw_rules "$tracker_content"

    # 5. Remove systemd services
    rollback_systemd_services "$tracker_content"

    # 6. Restore modified files from backups
    rollback_file_modifications "$tracker_content"

    # 7. Remove created directories
    rollback_directories "$tracker_content"

    # 8. Remove created files
    rollback_files "$tracker_content"

    # 9. Remove created user (only if we created it)
    rollback_user "$tracker_content" "$service_user"

    # 10. Remove aeonctl
    rollback_aeonctl

    success "Rollback completed successfully"
}

rollback_docker_containers() {
    local tracker_content="$1"
    local containers=$(echo "$tracker_content" | jq -r '.tracked.docker_containers_created[]?' 2>/dev/null || true)

    if [[ -z "$containers" ]]; then
        log "No Docker containers to rollback"
        return 0
    fi

    step "Rolling back Docker containers"

    for container in $containers; do
        if docker ps -a --format '{{.Names}}' | grep -q "^${container}$"; then
            log "Removing container: $container"
            run "docker rm -f \"$container\""
            success "Removed container: $container"
        else
            debug "Container $container not found, skipping"
        fi
    done
}

rollback_docker_images_built() {
    local tracker_content="$1"
    local images=$(echo "$tracker_content" | jq -r '.tracked.docker_images_built[]?' 2>/dev/null || true)

    if [[ -z "$images" ]]; then
        log "No Docker built images to rollback"
        return 0
    fi

    step "Rolling back Docker built images"

    for image in $images; do
        if docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^${image}$"; then
            log "Removing built image: $image"
            run "docker rmi \"$image\" 2>/dev/null || true"
            success "Removed built image: $image"
        else
            debug "Image $image not found, skipping"
        fi
    done

    # Note: We do NOT remove pulled images as they may be used by other containers
}

rollback_cron_jobs() {
    local tracker_content="$1"
    local cron_jobs=$(echo "$tracker_content" | jq -r '.tracked.cron_jobs_added[]?' 2>/dev/null || true)

    if [[ -z "$cron_jobs" ]]; then
        log "No cron jobs to rollback"
        return 0
    fi

    step "Rolling back cron jobs"

    for cron_file in $cron_jobs; do
        if [[ -f "$cron_file" ]]; then
            log "Removing cron file: $cron_file"
            run "rm -f \"$cron_file\""
            success "Removed cron file: $cron_file"
        fi
    done
}

rollback_ufw_rules() {
    local tracker_content="$1"
    local rules=$(echo "$tracker_content" | jq -r '.tracked.ufw_rules_added[]?' 2>/dev/null || true)

    if [[ -z "$rules" ]]; then
        log "No UFW rules to rollback"
        return 0
    fi

    if ! command -v ufw &> /dev/null; then
        warn "UFW not installed, cannot rollback rules"
        return 0
    fi

    step "Rolling back UFW rules"

    for rule in $rules; do
        log "Removing UFW rule: $rule"
        run "ufw delete $rule 2>/dev/null || true"
    done

    success "UFW rules rolled back"
}

rollback_systemd_services() {
    local tracker_content="$1"
    local services=$(echo "$tracker_content" | jq -r '.tracked.systemd_services_installed[]?' 2>/dev/null || true)

    if [[ -z "$services" ]]; then
        log "No systemd services to rollback"
        return 0
    fi

    step "Rolling back systemd services"

    for service in $services; do
        if systemctl list-unit-files | grep -q "^${service}\.service"; then
            log "Stopping and disabling service: $service"
            run "systemctl stop \"$service\" 2>/dev/null || true"
            run "systemctl disable \"$service\" 2>/dev/null || true"
            run "rm -f \"/etc/systemd/system/${service}.service\""
            run "systemctl daemon-reload"
            success "Removed systemd service: $service"
        else
            debug "Service $service not found, skipping"
        fi
    done
}

rollback_file_modifications() {
    local tracker_content="$1"
    local modifications=$(echo "$tracker_content" | jq -c '.tracked.files_modified[]?' 2>/dev/null || true)

    if [[ -z "$modifications" ]]; then
        log "No file modifications to rollback"
        return 0
    fi

    step "Restoring modified files from backups"

    while IFS= read -r mod; do
        local original=$(echo "$mod" | jq -r '.original')
        local backup=$(echo "$mod" | jq -r '.backup')

        if [[ -f "$backup" ]]; then
            log "Restoring $original from backup"
            run "cp \"$backup\" \"$original\""
            success "Restored: $original"
        else
            warn "Backup not found for $original: $backup"
        fi
    done <<< "$modifications"
}

rollback_directories() {
    local tracker_content="$1"
    local dirs=$(echo "$tracker_content" | jq -r '.tracked.dirs_created[]?' 2>/dev/null || true)

    if [[ -z "$dirs" ]]; then
        log "No directories to rollback"
        return 0
    fi

    step "Rolling back directories"

    # Sort by depth (deepest first) to remove children before parents
    local sorted_dirs=$(echo "$dirs" | tr ' ' '\n' | awk '{print length, $0}' | sort -rn | cut -d' ' -f2-)

    for dir in $sorted_dirs; do
        if [[ -d "$dir" ]]; then
            # Only remove if empty or only contains our tracked files
            if [[ -z "$(ls -A "$dir" 2>/dev/null)" ]]; then
                log "Removing empty directory: $dir"
                run "rmdir \"$dir\""
                success "Removed directory: $dir"
            else
                debug "Directory $dir not empty, preserving"
            fi
        fi
    done
}

rollback_files() {
    local tracker_content="$1"
    local files=$(echo "$tracker_content" | jq -r '.tracked.files_created[]?' 2>/dev/null || true)

    if [[ -z "$files" ]]; then
        log "No files to rollback"
        return 0
    fi

    step "Rolling back created files"

    for file in $files; do
        if [[ -f "$file" ]]; then
            log "Removing file: $file"
            run "rm -f \"$file\""
            success "Removed file: $file"
        fi
    done
}

rollback_user() {
    local tracker_content="$1"
    local service_user="$2"
    local users=$(echo "$tracker_content" | jq -r '.tracked.users_created[]?' 2>/dev/null || true)

    if [[ -z "$users" ]]; then
        log "No users to rollback"
        return 0
    fi

    step "Rolling back created user"

    for user in $users; do
        if id "$user" &>/dev/null; then
            # Check if user has any processes running
            if pgrep -u "$user" >/dev/null; then
                warn "User $user has running processes, not removing"
            else
                log "Removing user: $user"
                run "userdel \"$user\" 2>/dev/null || true"
                success "Removed user: $user"
            fi
        fi
    done
}

rollback_aeonctl() {
    local aeonctl_path="/usr/local/bin/aeonctl"

    if [[ -f "$aeonctl_path" ]]; then
        step "Removing aeonctl"
        run "rm -f \"$aeonctl_path\""
        success "Removed aeonctl"
    fi
}

# ------------------------------------------------------------------------------
# Tracking Helper Functions
# ------------------------------------------------------------------------------

track_file_created() {
    local file="$1"
    add_to_tracker "files_created" "$file"
}

track_dir_created() {
    local dir="$1"
    add_to_tracker "dirs_created" "$dir"
}

track_user_created() {
    local user="$1"
    add_to_tracker "users_created" "$user"
}

track_systemd_service() {
    local service="$1"
    add_to_tracker "systemd_services_installed" "$service"
}

track_docker_image_pulled() {
    local image="$1"
    add_to_tracker "docker_images_pulled" "$image"
}

track_docker_image_built() {
    local image="$1"
    add_to_tracker "docker_images_built" "$image"
}

track_docker_container() {
    local container="$1"
    add_to_tracker "docker_containers_created" "$container"
}

track_cron_job() {
    local cron_file="$1"
    add_to_tracker "cron_jobs_added" "$cron_file"
}

track_ufw_rule() {
    local rule="$1"
    add_to_tracker "ufw_rules_added" "$rule"
}

track_file_modified() {
    local original="$1"
    local backup="$2"
    add_file_backup "$original" "$backup"
}

# ------------------------------------------------------------------------------
# Phase 1: System Checks
# ------------------------------------------------------------------------------
check_root() {
    step "Checking privileges"
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)"
        exit 1
    fi
    success "Running as root"
}

check_os() {
    step "Checking operating system"
    if [[ ! -f /etc/os-release ]]; then
        error "Cannot determine OS version"
        exit 1
    fi
    source /etc/os-release
    if [[ "$ID" != "ubuntu" ]] || [[ "$VERSION_ID" != "24.04" ]]; then
        warn "This script is designed for Ubuntu 24.04 LTS (detected: $PRETTY_NAME)"
        if [[ "$DRY_RUN" != "true" ]]; then
            read -p "Continue anyway? [y/N] " -n 1 -r
            echo
            [[ $REPLY =~ ^[Yy]$ ]] || exit 1
        fi
    fi
    success "OS: $PRETTY_NAME"
}

check_arch() {
    step "Checking architecture"
    local arch=$(uname -m)
    if [[ "$arch" != "x86_64" ]]; then
        error "Unsupported architecture: $arch (requires x86_64)"
        exit 1
    fi
    success "Architecture: $arch"
}

check_resources() {
    step "Checking system resources"

    # RAM
    local ram_gb=$(free -g | awk '/^Mem:/{print $2}')
    if [[ $ram_gb -lt 2 ]]; then
        warn "Low RAM: ${ram_gb}GB (recommended: 4GB+)"
    else
        success "RAM: ${ram_gb}GB"
    fi

    # Disk space
    local disk_gb=$(df -BG / | awk 'NR==2 {print $4}' | sed 's/G//')
    if [[ $disk_gb -lt 10 ]]; then
        warn "Low disk space: ${disk_gb}GB available (recommended: 20GB+)"
    else
        success "Disk space: ${disk_gb}GB available"
    fi

    # CPU
    local cpu_cores=$(nproc)
    success "CPU cores: $cpu_cores"
}

check_docker() {
    step "Checking Docker"
    if ! command -v docker &> /dev/null; then
        warn "Docker not installed"
        set_pre_existing "docker_installed" false
        return 1
    fi

    if ! docker compose version &> /dev/null; then
        warn "Docker Compose plugin not available"
        return 1
    fi

    local docker_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
    success "Docker: $docker_version"
    success "Docker Compose: $(docker compose version --short 2>/dev/null || echo 'unknown')"
    set_pre_existing "docker_installed" true
    return 0
}

check_existing_services() {
    step "Checking for existing services"

    # Check for n8n
    if docker ps --format '{{.Names}}' | grep -q '^n8n$'; then
        warn "Existing n8n container detected - will preserve"
        set_pre_existing "n8n_container" true
        PRE_EXISTING_N8N=true
    else
        set_pre_existing "n8n_container" false
        PRE_EXISTING_N8N=false
    fi

    # Check for Caddy
    if docker ps --format '{{.Names}}' | grep -q 'caddy'; then
        warn "Existing Caddy container detected - will not modify"
        set_pre_existing "caddy_container" true
        PRE_EXISTING_CADDY=true
    elif systemctl is-active --quiet caddy 2>/dev/null; then
        warn "Existing Caddy service detected - will not modify"
        set_pre_existing "caddy_service" true
        PRE_EXISTING_CADDY=true
    else
        set_pre_existing "caddy_container" false
        set_pre_existing "caddy_service" false
        PRE_EXISTING_CADDY=false
    fi

    # Check occupied ports
    local occupied_ports=()
    for port in 8080 80 443; do
        if ss -ltn "sport = :$port" | grep -q ":$port "; then
            occupied_ports+=($port)
        fi
    done

    if [[ ${#occupied_ports[@]} -gt 0 ]]; then
        warn "Ports in use: ${occupied_ports[*]}"
        for port in "${occupied_ports[@]}"; do
            local proc=$(ss -ltnp "sport = :$port" | awk 'NR>1 {print $NF}' | head -1)
            debug "  Port $port: $proc"
        done
    else
        success "Required ports (8080, 80, 443) are free"
    fi
}

check_python() {
    step "Checking Python"
    if ! command -v python3 &> /dev/null; then
        error "Python 3 not installed"
        exit 1
    fi
    local py_version=$(python3 --version | cut -d' ' -f2)
    success "Python: $py_version"
}

# ------------------------------------------------------------------------------
# Phase 2: Directory & User Setup
# ------------------------------------------------------------------------------
setup_user() {
    step "Setting up service user"
    if id "$SERVICE_USER" &>/dev/null; then
        success "User $SERVICE_USER already exists"
    else
        run "useradd -r -m -d \"$INSTALL_DIR\" -s /usr/sbin/nologin -c \"AEON by Rekt Service\" \"$SERVICE_USER\""
        track_user_created "$SERVICE_USER"
        success "Created user $SERVICE_USER"
    fi
}

setup_directories() {
    step "Setting up directories"

    local dirs=("$INSTALL_DIR" "$DATA_DIR" "$BACKUP_DIR" "$LOG_DIR")
    for dir in "${dirs[@]}"; do
        if [[ ! -d "$dir" ]]; then
            run "mkdir -p \"$dir\""
            track_dir_created "$dir"
        fi
    done

    run "chown -R \"$SERVICE_USER:$SERVICE_GROUP\" \"$INSTALL_DIR\" \"$LOG_DIR\""
    run "chmod 750 \"$DATA_DIR\" \"$BACKUP_DIR\""
    run "chmod 755 \"$LOG_DIR\""
    success "Directories created"
}

# ------------------------------------------------------------------------------
# Phase 3: Project Installation
# ------------------------------------------------------------------------------
install_project() {
    step "Installing project files"

    local src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    if [[ "$src_dir" != "$INSTALL_DIR" ]]; then
        # Track files that will be created
        local files_to_copy=$(rsync -avn --exclude='.env' --exclude='data/' --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' --exclude='.git/' "$src_dir/" "$INSTALL_DIR/" | tail -n +2 | head -n -1 | awk '{print $NF}' | grep -v '^$' || true)

        run "rsync -a --exclude='.env' --exclude='data/' --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' --exclude='.git/' \"$src_dir/\" \"$INSTALL_DIR/\""

        # Track created files (simplified - track the install dir)
        track_dir_created "$INSTALL_DIR"
        success "Project files copied to $INSTALL_DIR"
    else
        success "Project already at $INSTALL_DIR"
    fi

    run "chown -R \"$SERVICE_USER:$SERVICE_GROUP\" \"$INSTALL_DIR\""
    run "find \"$INSTALL_DIR\" -name '*.sh' -exec chmod +x {} \\;"
}

# ------------------------------------------------------------------------------
# Phase 4: Environment Configuration
# ------------------------------------------------------------------------------
setup_environment() {
    step "Configuring environment"

    local env_file="${INSTALL_DIR}/.env"
    local example_file="${INSTALL_DIR}/.env.example"

    if [[ ! -f "$example_file" ]]; then
        error ".env.example not found at $example_file"
        exit 1
    fi

    if [[ ! -f "$env_file" ]]; then
        run "cp \"$example_file\" \"$env_file\""
        track_file_created "$env_file"
        success "Created .env from example"
    else
        success "Existing .env preserved"
    fi

    run "chmod 600 \"$env_file\""
    run "chown \"$SERVICE_USER:$SERVICE_GROUP\" \"$env_file\""

    # Update .env with detected values - track modifications
    local hostname=$(hostname -f 2>/dev/null || hostname)

    if [[ -n "$DOMAIN" ]]; then
        backup_file "${env_file}"
        run "sed -i 's|^# DASHBOARD_HEALTH_URL=.*|DASHBOARD_HEALTH_URL=https://$DOMAIN/healthz|' \"$env_file\""
        run "sed -i 's|^# DASHBOARD_URL=.*|DASHBOARD_URL=https://$DOMAIN|' \"$env_file\""
    else
        backup_file "${env_file}"
        run "sed -i 's|^# DASHBOARD_HEALTH_URL=.*|DASHBOARD_HEALTH_URL=http://${hostname}:8080/healthz|' \"$env_file\""
        run "sed -i 's|^# DASHBOARD_URL=.*|DASHBOARD_URL=http://${hostname}:8080|' \"$env_file\""
    fi

    # Ensure DATA_DIR is set
    backup_file "${env_file}"
    run "sed -i 's|^DATA_DIR=.*|DATA_DIR=$DATA_DIR|' \"$env_file\""

    # Generate secure dashboard password if not set
    if ! grep -qE '^DASHBOARD_PASSWORD=.{8,}' "$env_file"; then
        backup_file "${env_file}"
        local gen_pass=$(openssl rand -base64 24 | tr -d "=+/" | cut -c1-20)
        run "sed -i \"s|^DASHBOARD_PASSWORD=.*|DASHBOARD_PASSWORD=$gen_pass|\" \"$env_file\""
        warn "Generated dashboard password: $gen_pass"
        warn "Save this password! It's in $env_file"
    fi

    success "Environment configured"
}

backup_file() {
    local file="$1"
    if [[ -f "$file" && "$DRY_RUN" != "true" ]]; then
        local backup="${file}.bak.$(date +%s)"
        cp "$file" "$backup"
        track_file_modified "$file" "$backup"
    fi
}

# ------------------------------------------------------------------------------
# Phase 5: Docker Setup
# ------------------------------------------------------------------------------
install_docker() {
    if [[ "$FORCE_DOCKER" != "true" ]]; then
        log "Skipping Docker installation (--no-docker flag)"
        return 0
    fi

    step "Installing Docker"

    if check_docker; then
        success "Docker already installed"
        return 0
    fi

    run "apt-get update -q"
    run "apt-get install -y -q ca-certificates curl gnupg lsb-release"

    run "install -m 0755 -d /etc/apt/keyrings"
    run "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg"
    run "chmod a+r /etc/apt/keyrings/docker.gpg"

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        | run "tee /etc/apt/sources.list.d/docker.list > /dev/null"

    run "apt-get update -q"
    run "apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"

    run "systemctl enable docker"
    run "systemctl start docker"

    success "Docker installed and started"
}

configure_docker_compose() {
    step "Configuring Docker Compose"

    local compose_file="${INSTALL_DIR}/docker-compose.yml"
    local override_file="${INSTALL_DIR}/docker-compose.override.yml"

    if [[ ! -f "$compose_file" ]]; then
        error "docker-compose.yml not found at $compose_file"
        exit 1
    fi

    # Backup original compose file (but don't modify it per audit requirements)
    backup_file "$compose_file"

    # Generate docker-compose.override.yml instead of modifying the base file
    # This keeps docker-compose.yml immutable as per audit requirements
    cat > "$override_file" << 'OVERRIDE_EOF'
# AEON by Rekt - Docker Compose Override
# This file extends the base docker-compose.yml with AEON-specific configuration.
# The base docker-compose.yml remains immutable.

services:
  bot:
    container_name: aeon-bot
    networks:
      - aeon-network
    environment:
      - DATA_DIR=/app/data
      - DASHBOARD_HEALTH_URL=http://dashboard:8080/healthz
      - DASHBOARD_SCREENSHOT_URL=http://dashboard:8080

  dashboard:
    container_name: aeon-dashboard
    networks:
      - aeon-network
    ports:
      - "${DASHBOARD_BIND:-127.0.0.1}:${PORT:-8080}:8080"
    environment:
      - PORT=8080
      - DATA_DIR=/app/data

OVERRIDE_EOF

    # Add optional services if needed
    cat >> "$override_file" << 'OVERRIDE_EOF'

  caddy:
    container_name: aeon-caddy
    profiles: ["https"]
    networks:
      - aeon-network
    volumes:
      - aeon_caddy_data:/data
      - aeon_caddy_config:/config

  cloudflared:
    container_name: aeon-tunnel
    profiles: ["tunnel"]
    networks:
      - aeon-network

  autoheal:
    container_name: aeon-autoheal
    profiles: ["autoheal"]

networks:
  aeon-network:
    driver: bridge
    name: aeon-network

volumes:
  aeon_caddy_data:
  aeon_caddy_config:
OVERRIDE_EOF

    # Update Caddyfile if using domain
    if [[ -n "$DOMAIN" ]]; then
        local caddyfile="${INSTALL_DIR}/Caddyfile"
        if [[ -f "$caddyfile" ]]; then
            backup_file "$caddyfile"
            run "sed -i \"s|{\$DOMAIN}|$DOMAIN|g\" \"$caddyfile\""
            run "sed -i \"s|email {\$ACME_EMAIL}|email $EMAIL|g\" \"$caddyfile\""
        fi
    fi

    # Track override file creation
    track_file_created "$override_file"
    success "Docker Compose configured (override file created, base file unchanged)"
}

build_and_start_docker() {
    step "Building and starting Docker services"

    # Track images before build
    local images_before=$(docker images --format '{{.Repository}}:{{.Tag}}' | sort)

    run "cd \"$INSTALL_DIR\" && docker compose build --pull"

    # Track images after build - find new ones
    local images_after=$(docker images --format '{{.Repository}}:{{.Tag}}' | sort)
    local new_images=$(comm -13 <(echo "$images_before") <(echo "$images_after"))

    for img in $new_images; do
        if [[ "$img" == aeon-* ]] || [[ "$img" == *"aeon"* ]]; then
            track_docker_image_built "$img"
        fi
    done

    # Track pulled images (base images)
    local base_images=$(docker history --format '{{.Image}}' $(docker compose config --images) 2>/dev/null | grep -v '<missing>' | sort -u)
    for img in $base_images; do
        if [[ -n "$img" ]]; then
            track_docker_image_pulled "$img"
        fi
    done

    run "cd \"$INSTALL_DIR\" && docker compose up -d"

    # Track created containers
    local containers=$(docker compose ps --format '{{.Names}}' 2>/dev/null || true)
    for container in $containers; do
        track_docker_container "$container"
    done

    # Wait for services to be healthy
    local max_wait=120
    local waited=0
    while [[ $waited -lt $max_wait ]]; do
        local bot_health=$(docker inspect --format='{{.State.Health.Status}}' aeon-bot 2>/dev/null || echo "starting")
        local dash_health=$(docker inspect --format='{{.State.Health.Status}}' aeon-dashboard 2>/dev/null || echo "starting")

        if [[ "$bot_health" == "healthy" && "$dash_health" == "healthy" ]]; then
            success "All services healthy"
            return 0
        fi

        sleep 5
        waited=$((waited + 5))
        debug "Waiting for health checks... (bot: $bot_health, dashboard: $dash_health)"
    done

    warn "Health checks not passing yet - checking logs"
    run "cd \"$INSTALL_DIR\" && docker compose logs --tail=50"
    return 1
}

# ------------------------------------------------------------------------------
# Phase 6: Systemd Alternative
# ------------------------------------------------------------------------------
install_systemd() {
    step "Installing systemd services"

    run "apt-get update -q"
    run "apt-get install -y -q python3-venv python3-pip curl ca-certificates fonts-dejavu-core fonts-noto-cjk fonts-noto-color-emoji"

    # Install Chrome for screenshots
    if ! command -v google-chrome-stable &> /dev/null && ! command -v chromium &> /dev/null; then
        if [[ "$(dpkg --print-architecture)" == "amd64" ]]; then
            run "curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
            run "apt-get install -y -q /tmp/chrome.deb && rm -f /tmp/chrome.deb"
        else
            run "apt-get install -y -q chromium || warn 'Chromium not available on ARM'"
        fi
    fi

    # Create venv and install deps
    run "cd \"$INSTALL_DIR\" && [ -x .venv/bin/python ] || python3 -m venv .venv"
    run "cd \"$INSTALL_DIR\" && .venv/bin/pip install -q --upgrade pip"
    run "cd \"$INSTALL_DIR\" && .venv/bin/pip install -q -r requirements.txt"
    run "chown -R \"$SERVICE_USER:$SERVICE_GROUP\" \"$INSTALL_DIR\""

    # Generate systemd service files from templates with proper variable substitution
    local bot_service="/etc/systemd/system/aeon-bot.service"
    local dash_service="/etc/systemd/system/aeon-dashboard.service"

    # Create aeon-bot.service from template
    sed -e "s|\${INSTALL_DIR}|$INSTALL_DIR|g" \
        -e "s|\${SERVICE_USER}|$SERVICE_USER|g" \
        -e "s|\${SERVICE_GROUP}|$SERVICE_GROUP|g" \
        -e "s|\${DATA_DIR}|$DATA_DIR|g" \
        "$INSTALL_DIR/systemd/aeon-bot.service.template" > "$bot_service"
    track_file_created "$bot_service"

    # Create aeon-dashboard.service from template
    sed -e "s|\${INSTALL_DIR}|$INSTALL_DIR|g" \
        -e "s|\${SERVICE_USER}|$SERVICE_USER|g" \
        -e "s|\${SERVICE_GROUP}|$SERVICE_GROUP|g" \
        -e "s|\${DATA_DIR}|$DATA_DIR|g" \
        "$INSTALL_DIR/systemd/aeon-dashboard.service.template" > "$dash_service"
    track_file_created "$dash_service"

    track_systemd_service "aeon-bot"
    track_systemd_service "aeon-dashboard"

    run "systemctl daemon-reload"
    run "systemctl enable aeon-bot aeon-dashboard"

    success "Systemd services installed from templates"
}

# ------------------------------------------------------------------------------
# Phase 7: Management CLI (aeonctl)
# ------------------------------------------------------------------------------
install_aeonctl() {
    step "Installing aeonctl management CLI"

    local aeonctl_path="/usr/local/bin/aeonctl"

    cat > "$aeonctl_path" <<'AEONCTL_EOF'
#!/usr/bin/env bash
# AEON by Rekt - Management CLI
set -euo pipefail

readonly PROJECT_NAME="aeon-by-rekt"
readonly INSTALL_DIR="/opt/aeon-by-rekt"
readonly DATA_DIR="${INSTALL_DIR}/data"
readonly COMPOSE_FILE="${INSTALL_DIR}/docker-compose.yml"
readonly SERVICE_BOT="aeon-bot"
readonly SERVICE_DASH="aeon-dashboard"

usage() {
    cat <<EOF
AEON by Rekt - Management CLI

Usage: aeonctl <command> [options]

Commands:
  install         Run installation (same as install.sh)
  start           Start all services
  stop            Stop all services
  restart         Restart all services
  status          Show service status
  health          Run health checks
  logs            Show logs (use -f to follow)
  update          Update to latest version
  backup          Create manual backup
  restore         Restore from backup
  test            Run test suite
  version         Show version
  config          Show/edit configuration
  uninstall       Uninstall AEON (with confirmation)

Options:
  -f, --follow    Follow logs (for logs command)
  -h, --help      Show this help

Examples:
  aeonctl start
  aeonctl logs -f
  aeonctl backup
  aeonctl restore backups-manuali/paper-bot-data-20241115-120000.tar.gz
  aeonctl status
EOF
}

check_docker() {
    if ! command -v docker &> /dev/null; then
        echo "Docker not available"
        return 1
    fi
    if ! docker compose version &> /dev/null; then
        echo "Docker Compose not available"
        return 1
    fi
    return 0
}

cmd_start() {
    echo "Starting AEON services..."
    if check_docker; then
        cd "$INSTALL_DIR" && docker compose up -d
    else
        systemctl start aeon-bot aeon-dashboard
    fi
    echo "Services started"
}

cmd_stop() {
    echo "Stopping AEON services..."
    if check_docker; then
        cd "$INSTALL_DIR" && docker compose stop
    else
        systemctl stop aeon-bot aeon-dashboard
    fi
    echo "Services stopped"
}

cmd_restart() {
    cmd_stop
    sleep 2
    cmd_start
}

cmd_status() {
    echo "=== AEON Service Status ==="
    if check_docker; then
        cd "$INSTALL_DIR" && docker compose ps
        echo ""
        echo "=== Health Checks ==="
        docker inspect --format='{{.Name}}: {{.State.Health.Status}}' "$SERVICE_BOT" "$SERVICE_DASH" 2>/dev/null || true
    else
        systemctl status aeon-bot aeon-dashboard --no-pager -l
    fi
}

cmd_health() {
    echo "=== AEON Health Checks ==="
    if check_docker; then
        python3 "$INSTALL_DIR/deploy/healthcheck.py" || true
        curl -sf http://127.0.0.1:8080/healthz | python3 -m json.tool || echo "Dashboard health check failed"
    else
        python3 "$INSTALL_DIR/deploy/healthcheck.py" || true
        curl -sf http://127.0.0.1:8080/healthz | python3 -m json.tool || echo "Dashboard health check failed"
    fi
}

cmd_logs() {
    local follow=""
    [[ "${1:-}" == "-f" || "${1:-}" == "--follow" ]] && follow="-f"

    if check_docker; then
        cd "$INSTALL_DIR" && docker compose logs $follow --tail=100
    else
        journalctl -u aeon-bot -u aeon-dashboard $follow --no-pager -n 100
    fi
}

cmd_update() {
    echo "Updating AEON..."
    # Backup first
    cmd_backup

    if check_docker; then
        cd "$INSTALL_DIR" && docker compose pull && docker compose build --pull && docker compose up -d
    else
        cd "$INSTALL_DIR" && git pull 2>/dev/null || true
        .venv/bin/pip install -q -r requirements.txt
        systemctl restart aeon-bot aeon-dashboard
    fi
    echo "Update complete"
}

cmd_backup() {
    echo "Creating backup..."
    bash "$INSTALL_DIR/scripts/backup.sh"
}

cmd_restore() {
    local backup_file="${1:-}"
    if [[ -z "$backup_file" ]]; then
        echo "Usage: aeonctl restore <backup-file>"
        exit 1
    fi
    bash "$INSTALL_DIR/scripts/restore.sh" "$backup_file"
}

cmd_test() {
    echo "Running test suite..."
    cd "$INSTALL_DIR"
    python3 scripts/test_dashboard.py
    python3 scripts/test_survival.py
    python3 scripts/test_commands.py 2>&1 | grep -E "(OK|FAIL|PASS|RESULT)"
    echo "Tests complete"
}

cmd_version() {
    echo "AEON by Rekt CLI v1.0.0"
    echo "Project: $(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo "unknown")"
}

cmd_config() {
    local env_file="$INSTALL_DIR/.env"
    if [[ "${1:-}" == "edit" ]]; then
        ${EDITOR:-nano} "$env_file"
    else
        echo "=== Current Configuration ==="
        grep -E '^[A-Z_]+=' "$env_file" | sed 's/=.*/=***/' | sort
    fi
}

cmd_uninstall() {
    echo "⚠️  WARNING: This will remove AEON by Rekt completely!"
    echo "This action cannot be undone."
    echo ""
    echo "This uninstaller follows a SAFE removal process:"
    echo "  Step 1: Stop and remove AEON containers only (preserves volumes/data)"
    echo "  Step 2: OPTIONAL - Remove AEON data volumes and directories (requires separate confirmation)"
    echo ""

    # ========================================================================
    # STEP 1: Confirm container removal (preserves data by default)
    # ========================================================================
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "STEP 1: Remove AEON containers (aeon-bot, aeon-dashboard, aeon-caddy, aeon-tunnel, aeon-autoheal)"
    echo "        This does NOT delete volumes or data directories."
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    read -p "Type 'REMOVE-CONTAINERS' to confirm container removal: " confirm_containers
    [[ "$confirm_containers" == "REMOVE-CONTAINERS" ]] || { echo "Cancelled - no changes made"; exit 1; }

    echo "Stopping and removing AEON containers..."
    cmd_stop

    if check_docker; then
        # ONLY stop and remove containers - NO -v flag (preserves volumes)
        cd "$INSTALL_DIR" && docker compose down --remove-orphans

        # Remove only AEON-specific images
        docker image rm aeon-bot:latest aeon-dashboard:latest 2>/dev/null || true

        # Remove AEON-specific volumes (only if they exist and are AEON-named)
        local aeon_volumes=$(docker volume ls --format '{{.Name}}' | grep '^aeon_' || true)
        if [[ -n "$aeon_volumes" ]]; then
            echo ""
            echo "Found AEON Docker volumes (will be preserved by default):"
            echo "$aeon_volumes"
            echo ""
            echo "To remove these volumes, run Step 2 below."
        fi
    else
        systemctl disable aeon-bot aeon-dashboard 2>/dev/null || true
        systemctl stop aeon-bot aeon-dashboard 2>/dev/null || true
        rm -f /etc/systemd/system/aeon-bot.service /etc/systemd/system/aeon-dashboard.service
        systemctl daemon-reload
    fi

    # Remove aeonctl
    rm -f /usr/local/bin/aeonctl

    success "Step 1 complete: AEON containers and services removed."
    echo "Data volumes and directories have been PRESERVED."
    echo ""

    # ========================================================================
    # STEP 2: Optional data removal with explicit confirmation
    # ========================================================================
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "STEP 2 (OPTIONAL): Remove AEON data volumes and directories"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "The following AEON-SPECIFIC resources would be PERMANENTLY DELETED:"
    echo ""

    local resources_to_delete=()
    local has_resources=false

    # Check for AEON Docker volumes
    if check_docker; then
        local aeon_volumes=$(docker volume ls --format '{{.Name}}' | grep '^aeon_' || true)
        if [[ -n "$aeon_volumes" ]]; then
            echo "  Docker Volumes (AEON-specific):"
            while IFS= read -r vol; do
                [[ -n "$vol" ]] && echo "    - $vol" && resources_to_delete+=("volume:$vol") && has_resources=true
            done <<< "$aeon_volumes"
        fi
    fi

    # Check for AEON data directory
    if [[ -d "$INSTALL_DIR/data" ]]; then
        echo "  Data Directory:"
        echo "    - $INSTALL_DIR/data"
        resources_to_delete+=("dir:$INSTALL_DIR/data")
        has_resources=true
    fi

    # Check for AEON backup directory
    if [[ -d "$INSTALL_DIR/backups-manuali" ]]; then
        echo "  Backup Directory:"
        echo "    - $INSTALL_DIR/backups-manuali"
        resources_to_delete+=("dir:$INSTALL_DIR/backups-manuali")
        has_resources=true
    fi

    # Check for AEON log directory
    if [[ -d "/var/log/aeon-by-rekt" ]]; then
        echo "  Log Directory:"
        echo "    - /var/log/aeon-by-rekt"
        resources_to_delete+=("dir:/var/log/aeon-by-rekt")
        has_resources=true
    fi

    # Check for AEON install directory (config, scripts, etc.)
    if [[ -d "$INSTALL_DIR" ]]; then
        echo "  Installation Directory (config, scripts, compose files):"
        echo "    - $INSTALL_DIR"
        resources_to_delete+=("dir:$INSTALL_DIR")
        has_resources=true
    fi

    # Check for aeon service user
    if id "$SERVICE_USER" &>/dev/null; then
        echo "  Service User:"
        echo "    - $SERVICE_USER"
        resources_to_delete+=("user:$SERVICE_USER")
        has_resources=true
    fi

    echo ""
    echo "The following will NOT be touched (preserved):"
    echo "  ✓ n8n containers/volumes"
    echo "  ✓ Pre-existing Caddy containers/volumes"
    echo "  ✓ Docker installation"
    echo "  ✓ System Docker volumes"
    echo "  ✓ Other project data"
    echo ""

    if [[ "$has_resources" == "false" ]]; then
        echo "No AEON data resources found to remove."
        echo "Uninstall complete."
        exit 0
    fi

    echo "⚠️  THIS ACTION IS IRREVERSIBLE - DATA WILL BE PERMANENTLY LOST"
    echo ""
    echo "A BACKUP will be created before deletion at: $BACKUP_DIR/pre-uninstall-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
    echo ""
    read -p "Type 'DELETE-DATA' to confirm PERMANENT data deletion: " confirm_data
    [[ "$confirm_data" == "DELETE-DATA" ]] || { echo "Cancelled - data preserved at $INSTALL_DIR"; exit 0; }

    echo ""
    echo "Creating backup before deletion..."

    # Create backup of data directories
    local backup_file="$BACKUP_DIR/pre-uninstall-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
    mkdir -p "$BACKUP_DIR"

    local backup_items=()
    [[ -d "$INSTALL_DIR/data" ]] && backup_items+=("$INSTALL_DIR/data")
    [[ -d "$INSTALL_DIR/backups-manuali" ]] && backup_items+=("$INSTALL_DIR/backups-manuali")
    [[ -d "/var/log/aeon-by-rekt" ]] && backup_items+=("/var/log/aeon-by-rekt")
    [[ -f "$INSTALL_DIR/.env" ]] && backup_items+=("$INSTALL_DIR/.env")
    [[ -f "$INSTALL_DIR/docker-compose.yml" ]] && backup_items+=("$INSTALL_DIR/docker-compose.yml")
    [[ -f "$INSTALL_DIR/Caddyfile" ]] && backup_items+=("$INSTALL_DIR/Caddyfile")

    if [[ ${#backup_items[@]} -gt 0 ]]; then
        tar -czf "$backup_file" "${backup_items[@]}" 2>/dev/null || warn "Some files could not be backed up"
        success "Backup created: $backup_file"
    else
        warn "No data to backup"
    fi

    echo ""
    echo "Deleting AEON data resources..."

    # Remove AEON Docker volumes
    if check_docker; then
        local aeon_volumes=$(docker volume ls --format '{{.Name}}' | grep '^aeon_' || true)
        for vol in $aeon_volumes; do
            [[ -n "$vol" ]] && docker volume rm "$vol" 2>/dev/null && success "Removed volume: $vol" || true
        done
    fi

    # Remove directories
    for resource in "${resources_to_delete[@]}"; do
        local type="${resource%%:*}"
        local path="${resource#*:}"

        if [[ "$type" == "dir" && -d "$path" ]]; then
            rm -rf "$path"
            success "Removed directory: $path"
        elif [[ "$type" == "user" ]]; then
            if id "$path" &>/dev/null; then
                # Check if user has running processes
                if pgrep -u "$path" >/dev/null; then
                    warn "User $path has running processes, not removing"
                else
                    userdel "$path" 2>/dev/null && success "Removed user: $path" || warn "Could not remove user: $path"
                fi
            fi
        fi
    done

    success "AEON by Rekt completely removed."
    echo "Backup saved at: $backup_file"
}

# Main
command="${1:-}"
shift || true

case "$command" in
    install) exec sudo bash "$INSTALL_DIR/install.sh" "$@" ;;
    start) cmd_start ;;
    stop) cmd_stop ;;
    restart) cmd_restart ;;
    status) cmd_status ;;
    health) cmd_health ;;
    logs) cmd_logs "$@" ;;
    update) cmd_update ;;
    backup) cmd_backup ;;
    restore) cmd_restore "$@" ;;
    test) cmd_test ;;
    version) cmd_version ;;
    config) cmd_config "$@" ;;
    uninstall) cmd_uninstall ;;
    -h|--help|help|"") usage ;;
    *) echo "Unknown command: $command"; usage; exit 1 ;;
esac
AEONCTL_EOF

    track_file_created "$aeonctl_path"
    run "chmod +x \"$aeonctl_path\""
    success "aeonctl installed at $aeonctl_path"
}

# ------------------------------------------------------------------------------
# Phase 8: Additional Scripts
# ------------------------------------------------------------------------------
install_healthcheck() {
    step "Installing healthcheck script"

    local healthcheck_path="${INSTALL_DIR}/healthcheck.sh"

    cat > "$healthcheck_path" <<'HEALTHCHECK_EOF'
#!/usr/bin/env bash
# AEON Health Check Script
set -euo pipefail

readonly INSTALL_DIR="/opt/aeon-by-rekt"
readonly DATA_DIR="${INSTALL_DIR}/data"

check_bot() {
    python3 "${INSTALL_DIR}/deploy/healthcheck.py"
}

check_dashboard() {
    curl -sf http://127.0.0.1:8080/healthz | python3 -m json.tool
}

check_disk() {
    local usage=$(df -h "$DATA_DIR" | awk 'NR==2 {print $5}' | sed 's/%//')
    if [[ $usage -gt 90 ]]; then
        echo "DISK CRITICAL: ${usage}% used"
        return 1
    elif [[ $usage -gt 80 ]]; then
        echo "DISK WARNING: ${usage}% used"
        return 1
    fi
    echo "DISK OK: ${usage}% used"
}

check_memory() {
    local mem=$(free | awk '/^Mem:/{printf "%.0f", $3/$2*100}')
    if [[ $mem -gt 90 ]]; then
        echo "MEMORY CRITICAL: ${mem}% used"
        return 1
    fi
    echo "MEMORY OK: ${mem}% used"
}

main() {
    local failed=0

    echo "=== AEON Health Check ==="
    echo "Time: $(date)"
    echo ""

    echo "--- Bot ---"
    check_bot || failed=1

    echo ""
    echo "--- Dashboard ---"
    check_dashboard || failed=1

    echo ""
    echo "--- System ---"
    check_disk || failed=1
    check_memory || failed=1

    echo ""
    if [[ $failed -eq 0 ]]; then
        echo "✅ All checks passed"
        exit 0
    else
        echo "❌ Some checks failed"
        exit 1
    fi
}

main "$@"
HEALTHCHECK_EOF

    track_file_created "$healthcheck_path"
    run "chmod +x \"$healthcheck_path\""
    success "Healthcheck script installed"
}

install_update_script() {
    step "Installing update script"

    local update_path="${INSTALL_DIR}/update.sh"

    cat > "$update_path" <<'UPDATE_EOF'
#!/usr/bin/env bash
# AEON Update Script
set -euo pipefail

readonly INSTALL_DIR="/opt/aeon-by-rekt"
readonly BACKUP_DIR="${INSTALL_DIR}/backups-manuali"

main() {
    echo "=== AEON Update ==="
    echo "Creating pre-update backup..."
    bash "${INSTALL_DIR}/scripts/backup.sh"

    echo "Pulling latest images..."
    cd "$INSTALL_DIR"
    docker compose pull
    docker compose build --pull

    echo "Restarting services..."
    docker compose up -d

    echo "Waiting for health checks..."
    sleep 10
    bash "${INSTALL_DIR}/healthcheck.sh"

    echo "✅ Update complete"
}

main "$@"
UPDATE_EOF

    track_file_created "$update_path"
    run "chmod +x \"$update_path\""
    success "Update script installed"
}

# ------------------------------------------------------------------------------
# Phase 9: Backup/Restore Enhancement
# ------------------------------------------------------------------------------
enhance_backup_restore() {
    step "Enhancing backup/restore scripts"

    # backup.sh is already good, ensure it's executable
    run "chmod +x \"${INSTALL_DIR}/scripts/backup.sh\""
    run "chmod +x \"${INSTALL_DIR}/scripts/restore.sh\""

    # Create a backup cron job
    local cron_file="/etc/cron.d/aeon-backup"
    cat > "$cron_file" <<CRON_EOF
# AEON Daily Backup at 03:00
0 3 * * * ${SERVICE_USER} cd ${INSTALL_DIR} && bash scripts/backup.sh >> ${LOG_DIR}/backup.log 2>&1
CRON_EOF

    track_cron_job "$cron_file"
    success "Backup scripts enhanced and cron scheduled"
}

# ------------------------------------------------------------------------------
# Phase 10: Firewall & Security
# ------------------------------------------------------------------------------
configure_firewall() {
    step "Configuring firewall"

    if command -v ufw &> /dev/null; then
        # Capture current UFW status and rules BEFORE making changes
        local ufw_status_output=$(ufw status verbose 2>/dev/null || echo "")
        local ufw_is_active=false
        if echo "$ufw_status_output" | grep -q "Status: active"; then
            ufw_is_active=true
        fi

        # Check existing rules to preserve them
        local existing_rules=$(ufw status numbered 2>/dev/null | grep -E "^\[" || echo "")
        debug "Current UFW status: active=$ufw_is_active"
        debug "Existing rules:\n$existing_rules"

        # Check if OpenSSH is already allowed (prevent lockout)
        local ssh_allowed=false
        if ufw status | grep -qE "OpenSSH|22/tcp.*ALLOW"; then
            ssh_allowed=true
            success "SSH (OpenSSH/port 22) already allowed in UFW"
        fi

        # Dry-run/analysis mode: show what would be changed
        if [[ "$DRY_RUN" == "true" ]]; then
            warn "=== FIREWALL DRY-RUN ANALYSIS ==="
            warn "Current UFW status: $([ "$ufw_is_active" = true ] && echo "ACTIVE" || echo "INACTIVE")"

            if [[ "$ssh_allowed" = false ]]; then
                warn "Would ADD: ufw allow OpenSSH (CRITICAL - prevents lockout)"
            else
                warn "Already present: OpenSSH allowed"
            fi

            warn "Would ADD: ufw allow from 127.0.0.1 to any port 8080 (dashboard localhost access)"

            if [[ -n "$DOMAIN" ]]; then
                warn "Would ADD: ufw allow 80/tcp (HTTP for Caddy/Let's Encrypt)"
                warn "Would ADD: ufw allow 443/tcp (HTTPS for Caddy)"
            else
                warn "No domain provided - HTTP/HTTPS NOT opened (Caddy not configured)"
            fi

            warn "Would REMOVE: ufw allow from 172.16.0.0/12 (broad Docker network rule - REMOVED per security policy)"

            if [[ "$ufw_is_active" = false ]]; then
                warn "Would ENABLE UFW after adding rules (ufw --force enable)"
            fi

            warn "=== END DRY-RUN ==="
            success "Firewall analysis complete (dry-run mode - no changes made)"
            return 0
        fi

        # ===== ACTUAL FIREWALL CHANGES (non-dry-run) =====

        # 1. Ensure SSH is allowed FIRST (critical - prevents lockout)
        if [[ "$ssh_allowed" = false ]]; then
            run "ufw allow OpenSSH"
            track_ufw_rule "allow OpenSSH"
            success "Added UFW rule: allow OpenSSH"
        fi

        # 2. Allow dashboard only on localhost (for SSH tunnel access)
        # Check if already exists
        if ! ufw status | grep -qE "127\.0\.0\.1.*8080|8080.*127\.0\.0\.1"; then
            run "ufw allow from 127.0.0.1 to any port 8080"
            track_ufw_rule "allow from 127.0.0.1 to any port 8080"
            success "Added UFW rule: allow localhost to port 8080 (dashboard)"
        else
            debug "Rule for localhost:8080 already exists"
        fi

        # 3. Allow HTTP/HTTPS ONLY if domain is provided (for Caddy)
        if [[ -n "$DOMAIN" ]]; then
            # Check HTTP
            if ! ufw status | grep -qE "80/tcp.*ALLOW|ALLOW.*80/tcp"; then
                run "ufw allow 80/tcp"
                track_ufw_rule "allow 80/tcp"
                success "Added UFW rule: allow HTTP (port 80) for Caddy/Let's Encrypt"
            else
                debug "HTTP (80/tcp) already allowed"
            fi

            # Check HTTPS
            if ! ufw status | grep -qE "443/tcp.*ALLOW|ALLOW.*443/tcp"; then
                run "ufw allow 443/tcp"
                track_ufw_rule "allow 443/tcp"
                success "Added UFW rule: allow HTTPS (port 443) for Caddy"
            else
                debug "HTTPS (443/tcp) already allowed"
            fi
        else
            log "No --domain provided: HTTP/HTTPS ports NOT opened (dashboard via SSH tunnel only)"
        fi

        # 4. REMOVED: Broad Docker network rule (172.16.0.0/12)
        # This was a security risk - allowing entire RFC1918 private range
        # Docker containers communicate via Docker's internal bridge network,
        # which doesn't require host firewall rules for inter-container communication.
        # If the broad rule exists from a previous install, we note it but don't re-add.
        if ufw status | grep -qE "172\.16\.0\.0/12"; then
            warn "Legacy broad rule '172.16.0.0/12' detected from previous install."
            warn "This rule allows ALL traffic from the entire 172.16.0.0/12 private range."
            warn "Consider removing manually: 'ufw delete allow from 172.16.0.0/12'"
            warn "We will NOT re-add this overly permissive rule."
        fi

        # 5. Enable UFW ONLY after verifying SSH is allowed
        # Never auto-enable without confirming SSH access is preserved
        if [[ "$ufw_is_active" = false ]]; then
            if [[ "$ssh_allowed" = true ]] || ufw status | grep -qE "OpenSSH|22/tcp.*ALLOW"; then
                warn "Enabling UFW firewall (SSH access verified)..."
                run "ufw --force enable"
                success "UFW enabled"
            else
                error "CRITICAL: UFW not enabled - SSH access not confirmed!"
                error "Add SSH rule first: ufw allow OpenSSH"
                error "Then re-run installation or enable manually: ufw --force enable"
                return 1
            fi
        else
            success "UFW already active"
        fi

        success "UFW configured securely"
    else
        warn "UFW not installed, skipping firewall config"
        if [[ "$DRY_RUN" != "true" ]]; then
            run "apt-get install -y -q ufw"
            configure_firewall
        fi
    fi
}

# ------------------------------------------------------------------------------
# Phase 11: Final Verification
# ------------------------------------------------------------------------------
verify_installation() {
    step "Verifying installation"

    local failed=0

    # Check services
    if check_docker; then
        if docker ps --format '{{.Names}}' | grep -q '^aeon-bot$'; then
            success "Bot container running"
        else
            error "Bot container not running"
            failed=1
        fi

        if docker ps --format '{{.Names}}' | grep -q '^aeon-dashboard$'; then
            success "Dashboard container running"
        else
            error "Dashboard container not running"
            failed=1
        fi

        # Health checks
        sleep 5
        if bash "${INSTALL_DIR}/healthcheck.sh"; then
            success "Health checks passed"
        else
            warn "Some health checks failed"
            failed=1
        fi
    else
        if systemctl is-active --quiet aeon-bot; then
            success "Bot service running"
        else
            error "Bot service not running"
            failed=1
        fi

        if systemctl is-active --quiet aeon-dashboard; then
            success "Dashboard service running"
        else
            error "Dashboard service not running"
            failed=1
        fi
    fi

    # Check data directory
    if [[ -f "${DATA_DIR}/state.json" ]]; then
        success "State file exists"
    else
        warn "State file not yet created (bot may be starting)"
    fi

    # Check .env
    if [[ -f "${INSTALL_DIR}/.env" ]]; then
        success ".env configured"
    else
        error ".env missing"
        failed=1
    fi

    # Check aeonctl
    if command -v aeonctl &> /dev/null; then
        success "aeonctl available"
    else
        error "aeonctl not in PATH"
        failed=1
    fi

    if [[ $failed -eq 0 ]]; then
        success "All verification checks passed"
        return 0
    else
        error "Some verification checks failed"
        return 1
    fi
}

print_summary() {
    step "Installation Summary"

    cat <<EOF

${GREEN}╔════════════════════════════════════════════════════════════════════════╗
║                    AEON by Rekt - Installation Complete                  ║
╚════════════════════════════════════════════════════════════════════════╝${NC}

${CYAN}Installation Directory:${NC} ${INSTALL_DIR}
${CYAN}Data Directory:${NC} ${DATA_DIR}
${CYAN}Backup Directory:${NC} ${BACKUP_DIR}
${CYAN}Log Directory:${NC} ${LOG_DIR}
${CYAN}Service User:${NC} ${SERVICE_USER}

${CYAN}Management Commands:${NC}
  aeonctl start        - Start all services
  aeonctl stop         - Stop all services
  aeonctl restart      - Restart all services
  aeonctl status       - Show service status
  aeonctl health       - Run health checks
  aeonctl logs [-f]    - Show logs (follow with -f)
  aeonctl update       - Update to latest version
  aeonctl backup       - Create manual backup
  aeonctl restore <f>  - Restore from backup
  aeonctl test         - Run test suite
  aeonctl config [edit]- Show/edit configuration
  aeonctl uninstall    - Uninstall AEON

${CYAN}Dashboard Access:${NC}
EOF

    if [[ -n "$DOMAIN" ]]; then
        echo "  https://$DOMAIN"
        echo "  (HTTPS via Caddy with Let's Encrypt)"
    else
        local hostname=$(hostname -f 2>/dev/null || hostname)
        echo "  http://${hostname}:8080 (via SSH tunnel)"
        echo "  ssh -L 8080:127.0.0.1:8080 user@${hostname}"
        echo "  Then open http://localhost:8080 in browser"
    fi

    echo ""
    echo "${CYAN}Dashboard Credentials:${NC}"
    echo "  User: $(grep '^DASHBOARD_USER=' "${INSTALL_DIR}/.env" | cut -d= -f2)"
    echo "  Password: (see ${INSTALL_DIR}/.env)"

    echo ""
    echo "${CYAN}Important Files:${NC}"
    echo "  ${INSTALL_DIR}/.env           - Configuration (keep secure!)"
    echo "  ${DATA_DIR}/                  - Persistent data (state, trades, logs)"
    echo "  ${BACKUP_DIR}/                - Manual backups"
    echo "  ${LOG_DIR}/                   - Service logs"

    echo ""
    echo "${CYAN}Rollback Information:${NC}"
    echo "  Rollback tracker: ${ROLLBACK_TRACKER}"
    echo "  To rollback: sudo bash install.sh --rollback ${ROLLBACK_TRACKER}"

    echo ""
    echo "${CYAN}Next Steps:${NC}"
    echo "  1. Configure API keys in ${INSTALL_DIR}/.env (optional)"
    echo "  2. Set up Telegram bot for notifications"
    echo "  3. Configure Vybe API key for copy-trading"
    echo "  4. Run 'aeonctl health' to verify everything works"

    if [[ -n "$DOMAIN" ]]; then
        echo "  5. DNS: Point $DOMAIN to this server's IP"
    fi

    echo ""
    echo "${YELLOW}⚠️  Remember: This is PAPER TRADING only - no real money involved${NC}"
    echo ""
}

# ------------------------------------------------------------------------------
# Idempotency Check Functions
# ------------------------------------------------------------------------------

is_idempotent() {
    # Check if installation is already complete and matches desired state
    local checks_passed=0
    local total_checks=0

    # Check user exists
    ((total_checks++))
    if id "$SERVICE_USER" &>/dev/null; then
        ((checks_passed++))
    fi

    # Check directories exist
    ((total_checks++))
    if [[ -d "$INSTALL_DIR" && -d "$DATA_DIR" && -d "$BACKUP_DIR" && -d "$LOG_DIR" ]]; then
        ((checks_passed++))
    fi

    # Check .env exists
    ((total_checks++))
    if [[ -f "${INSTALL_DIR}/.env" ]]; then
        ((checks_passed++))
    fi

    # Check aeonctl exists
    ((total_checks++))
    if command -v aeonctl &> /dev/null; then
        ((checks_passed++))
    fi

    # Check cron job
    ((total_checks++))
    if [[ -f "/etc/cron.d/aeon-backup" ]]; then
        ((checks_passed++))
    fi

    # Check services running (docker or systemd)
    ((total_checks++))
    if check_docker; then
        if docker ps --format '{{.Names}}' | grep -q '^aeon-bot$' && docker ps --format '{{.Names}}' | grep -q '^aeon-dashboard$'; then
            ((checks_passed++))
        fi
    else
        if systemctl is-active --quiet aeon-bot && systemctl is-active --quiet aeon-dashboard; then
            ((checks_passed++))
        fi
    fi

    debug "Idempotency check: $checks_passed/$total_checks checks passed"

    if [[ $checks_passed -eq $total_checks ]]; then
        return 0
    else
        return 1
    fi
}

# ------------------------------------------------------------------------------
# Main Installation Flow
# ------------------------------------------------------------------------------
main() {
    # Handle rollback mode first
    if [[ "${1:-}" == "--rollback" ]]; then
        parse_args "$@"
        perform_rollback
        exit 0
    fi

    echo -e "${BLUE}"
    cat <<'BANNER'
╔════════════════════════════════════════════════════════════════════════╗
║                    AEON by Rekt - VPS Installer                        ║
║                    Trading Intelligence Platform                        ║
╚════════════════════════════════════════════════════════════════════════╝
BANNER
    echo -e "${NC}"

    parse_args "$@"

    if [[ "$DRY_RUN" == "true" ]]; then
        warn "DRY RUN MODE - No changes will be made"
    fi

    # Initialize rollback tracker (unless dry-run)
    if [[ "$DRY_RUN" != "true" ]]; then
        init_rollback_tracker
    fi

    # Check idempotency - if already installed and no changes needed, skip
    if [[ "$DRY_RUN" != "true" ]] && is_idempotent; then
        log "Installation already complete and matches desired configuration"
        log "Running in idempotent mode - verifying and updating only..."
        # Still run verification and summary
        verify_installation
        print_summary
        exit 0
    fi

    # Phase 1: System Checks
    check_root
    check_os
    check_arch
    check_resources
    check_python
    check_existing_services

    # Phase 2: User & Directories
    setup_user
    setup_directories
    mark_phase_completed "directories"

    # Phase 3: Project Installation
    install_project
    mark_phase_completed "project"

    # Phase 4: Environment
    setup_environment
    mark_phase_completed "environment"

    # Phase 5: Docker or Systemd
    if [[ "$FORCE_DOCKER" == "true" ]]; then
        install_docker
        configure_docker_compose
        build_and_start_docker
        mark_phase_completed "docker"
    else
        install_systemd
        run "systemctl restart aeon-bot aeon-dashboard"
        mark_phase_completed "systemd"
    fi

    # Phase 6: Management CLI
    install_aeonctl
    mark_phase_completed "aeonctl"

    # Phase 7: Additional Scripts
    install_healthcheck
    install_update_script
    enhance_backup_restore
    mark_phase_completed "scripts"

    # Phase 8: Security
    configure_firewall
    mark_phase_completed "firewall"

    # Phase 9: Verification
    verify_installation
    mark_phase_completed "verification"

    # Summary
    print_summary
}

main "$@"