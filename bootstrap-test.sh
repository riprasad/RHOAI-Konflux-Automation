#!/bin/bash
set -e

# Default values
BRANCH="rhoai-3.2"
SKIP_SETUP=false

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

print_header() {
    echo -e "${BLUE}============================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}============================================${NC}"
}

print_step() {
    echo -e "${YELLOW}→ $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

usage() {
    echo -e "${YELLOW}Usage:${NC} $0 [--skip-setup] [--branch <branch-name>]"
    echo ""
    echo "Options:"
    echo "  --skip-setup          Skip setup steps and only run the operator-processor"
    echo "  --branch <branch>     Specify branch to work on (default: rhoai-3.2)"
    echo "  -h, --help            Show this help message"
    echo ""
    echo "Examples:"
    echo "  # Full setup + run (default branch: rhoai-3.2)"
    echo "  $0"
    echo ""
    echo "  # Full setup + run for a specific branch"
    echo "  $0 --branch rhoai-3.3"
    echo ""
    echo "  # Skip setup, only run the processor (branch must match the one used during setup)"
    echo "  $0 --skip-setup --branch rhoai-3.3"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-setup)
            SKIP_SETUP=true
            shift
            ;;
        --branch)
            if [[ -z "$2" ]]; then
                print_error "--branch requires an argument"
                usage
                exit 1
            fi
            BRANCH="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

# Absolute paths and shared config
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SANDBOX_DIR="$WORKSPACE_ROOT/operator-sandbox"

VENV_DIR="$SANDBOX_DIR/venv"
OPERATOR_PROCESSOR_SCRIPT_PATH="$SCRIPT_DIR/utils/processors/operator-processor.py"
REQUIREMENTS_FILE="$SCRIPT_DIR/utils/processors/requirements.txt"

GITHUB_ORG="red-hat-data-services"
RBC_REPO_NAME="RHOAI-Build-Config"
RHODS_OPERATOR_REPO_NAME="rhods-operator"
RBC_SPARSE_PATHS="bundle/bundle-patch.yaml"
RHODS_OPERATOR_SPARSE_PATHS="build,prefetched-manifests,.tekton"

# Derived paths
RHOAI_VERSION="v${BRANCH/rhoai-/}"
COMPONENT_SUFFIX="${RHOAI_VERSION/./-}"
ODH_OPERATOR_COMPONENT_NAME="odh-operator-${COMPONENT_SUFFIX}"

# Paths for operator processing
PATCH_YAML_PATH="${SANDBOX_DIR}/${RBC_REPO_NAME}/bundle/bundle-patch.yaml"
PUSH_PIPELINE_PATH="${SANDBOX_DIR}/${RHODS_OPERATOR_REPO_NAME}/.tekton/${ODH_OPERATOR_COMPONENT_NAME}-push.yaml"
OPERANDS_MAP_PATH="${SANDBOX_DIR}/${RHODS_OPERATOR_REPO_NAME}/build/operands-map.yaml"
NUDGING_YAML_PATH="${SANDBOX_DIR}/${RHODS_OPERATOR_REPO_NAME}/build/operator-nudging.yaml"
MANIFEST_CONFIG_PATH="${SANDBOX_DIR}/${RHODS_OPERATOR_REPO_NAME}/build/manifests-config.yaml"


# Sparse checkout function
sparse_checkout() {
    local repo_name="$1"
    local branch="$2"
    local sparse_paths="$3"
    local repo_url="http://github.com/${GITHUB_ORG}/${repo_name}.git"

    echo ""
    print_step "Repository: ${GITHUB_ORG}/${repo_name}"
    print_step "Branch: $branch"
    print_step "Sparse checkout: $sparse_paths"

    mkdir -p "$repo_name"
    cd "$repo_name"
    git init --quiet
    git remote add origin "$repo_url" 2>/dev/null || \
        git remote set-url origin "$repo_url"
    git config core.sparseCheckout true
    git config core.sparseCheckoutCone false
    echo "$sparse_paths" | tr ',' '\n' > .git/info/sparse-checkout
    git fetch --depth=1 origin "$branch" 2>&1 | grep -v "^From" || true
    git checkout FETCH_HEAD --quiet 2>/dev/null || git checkout -b "$branch" FETCH_HEAD --quiet
    cd "$SANDBOX_DIR"

    # Remove .git directory immediately after checkout
    rm -rf "$SANDBOX_DIR/$repo_name/.git"

    print_success "${repo_name} checked out"
}

if [[ "$SKIP_SETUP" != "true" ]]; then
    print_header "Setting up test environment for $BRANCH"
    echo ""

    # Prepare sandbox directory (clean up if exists)
    print_header "Preparing sandbox directory"
    echo ""
    if [[ -d "$SANDBOX_DIR" ]]; then
        print_step "Sandbox directory already exists at $SANDBOX_DIR, deleting for a fresh setup..."
        rm -rf "$SANDBOX_DIR"
        print_success "Existing sandbox directory removed."
    fi
    mkdir -p "$SANDBOX_DIR"
    print_success "Sandbox directory created: $SANDBOX_DIR"
    cd "$SANDBOX_DIR"
    echo ""

    # Checkout repositories
    print_header "Checking out $RBC_REPO_NAME repo"
    sparse_checkout "$RBC_REPO_NAME" "$BRANCH" "$RBC_SPARSE_PATHS"
    echo ""

    print_header "Checking out $RHODS_OPERATOR_REPO_NAME repo"
    sparse_checkout "$RHODS_OPERATOR_REPO_NAME" "$BRANCH" "$RHODS_OPERATOR_SPARSE_PATHS"
    echo ""

    # Setup Python virtual environment
    print_header "Setting up Python virtual environment"

    if [[ ! -f "$OPERATOR_PROCESSOR_SCRIPT_PATH" ]]; then
        print_error "operator-processor.py not found at $OPERATOR_PROCESSOR_SCRIPT_PATH"
        exit 1
    fi
    if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
        print_error "requirements.txt not found at $REQUIREMENTS_FILE"
        exit 1
    fi

    print_step "Creating Python virtual environment..."
    if [[ ! -d "$VENV_DIR" ]]; then
        python3 -m venv "$VENV_DIR"
        print_success "Virtual environment created: $VENV_DIR"
    else
        print_success "Virtual environment already exists: $VENV_DIR"
    fi

    print_step "Installing Python dependencies from $REQUIREMENTS_FILE..."
    "$VENV_DIR/bin/pip" install --quiet --default-timeout=100 -r "$REQUIREMENTS_FILE"
    print_success "Python dependencies installed"
    echo ""

    # Initialize git to track changes
    print_header "Initializing git repository to track changes"
    print_step "Initializing git repository in sandbox..."
    git init --quiet
    git config user.name "Bootstrap Test Setup" 2>/dev/null || true
    git config user.email "bootstrap@local.test" 2>/dev/null || true
    print_step "Adding all files to git..."
    git add -A
    git commit -m "Initial state before operator processor execution" --quiet
    print_success "Git repository initialized - changes will be tracked"
    echo ""
else
    print_header "Skipping setup, running operator-processor only!"
fi

# Run operator processor (always)
print_header "Running operator-processor"

# Set up Quay token
QUAY_TOKEN_FILE="${HOME}/.ssh/.rhoai-quay-api-token"
if [[ -f "${QUAY_TOKEN_FILE}" ]]; then
    export RHOAI_QUAY_API_TOKEN=$(cat "${QUAY_TOKEN_FILE}" | tr -d '[:space:]')
else
    print_error "Quay token file not found: ${QUAY_TOKEN_FILE}"
    exit 1
fi

cd "${SANDBOX_DIR}"
"${VENV_DIR}/bin/python" "${OPERATOR_PROCESSOR_SCRIPT_PATH}" -op process-operator-yamls \
    --patch-yaml-path "${PATCH_YAML_PATH}" \
    --operands-map-path "${OPERANDS_MAP_PATH}" \
    --nudging-yaml-path "${NUDGING_YAML_PATH}" \
    --manifest-config-path "${MANIFEST_CONFIG_PATH}" \
    --rhoai-version "${BRANCH}" \
    --push-pipeline-yaml-path "${PUSH_PIPELINE_PATH}" \
    --push-pipeline-operation enable

if [[ $? -eq 0 ]]; then
    print_success "Operator processing completed successfully"
else
    print_error "Operator processing failed"
    exit 1
fi

print_header "Bootstrap Complete!"
echo ""
print_success "All steps completed successfully!"
echo ""
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${YELLOW}  Next Steps:${NC}"
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  To view changes made by the processor:"
echo "    cd ${SANDBOX_DIR} && git diff"
echo ""
echo "  To re-run the processor (skip setup):"
echo "    $0 --skip-setup --branch ${BRANCH}"
echo ""
