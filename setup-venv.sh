#!/bin/bash
set -e

# Default values
BRANCH="rhoai-3.2"
RUN_ONLY=false

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
    echo -e "${YELLOW}Usage:${NC} $0 [--run] [--branch <branch-name>]"
    echo "  --run                 Only run the bundle-processor (skip setup steps)"
    echo "  --branch <branch>     Specify branch to work on (default: rhoai-3.2)"
    echo ""
    echo "If --run is omitted, full environment setup will be done."
}



# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --run)
            RUN_ONLY=true
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
SANDBOX_DIR="$WORKSPACE_ROOT/bundle-sandbox"
RBC_REPO_LINK="https://github.com/red-hat-data-services/RHOAI-Build-Config.git"
RBC_REPO_DIR="$SANDBOX_DIR/$BRANCH"
RAW_INPUTS_DIR="$SANDBOX_DIR/utils/tmp/bundle"
VENV_DIR="$SANDBOX_DIR/venv"
REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"
BUNDLE_PROCESSOR_SCRIPT="$SCRIPT_DIR/bundle-processor.py"

# Derive RHOAI_VERSION and other identifiers
RHOAI_VERSION="v${BRANCH/rhoai-/}"
COMPONENT_SUFFIX="${RHOAI_VERSION/./-}"
OPERATOR_BUNDLE_COMPONENT_NAME="odh-operator-bundle-${COMPONENT_SUFFIX}"

# Paths for bundle processing
BUILD_CONFIG_PATH="${SANDBOX_DIR}/${BRANCH}/config/build-config.yaml"
BUNDLE_CSV_PATH="${RAW_INPUTS_DIR}/manifests/rhods-operator.clusterserviceversion.yaml"
PATCH_YAML_PATH="${SANDBOX_DIR}/${BRANCH}/bundle/bundle-patch.yaml"
OUTPUT_FILE_PATH="${RAW_INPUTS_DIR}/manifests/rhods-operator.clusterserviceversion.yaml"
SNAPSHOT_JSON_PATH="${SANDBOX_DIR}/${BRANCH}/config/snapshot.json"
ANNOTATION_YAML_PATH="${RAW_INPUTS_DIR}/metadata/annotations.yaml"
PUSH_PIPELINE_PATH="${SANDBOX_DIR}/${BRANCH}/.tekton/${OPERATOR_BUNDLE_COMPONENT_NAME}-push.yaml"

if [[ "$RUN_ONLY" != "true" ]]; then
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
    if [[ -d "$SCRIPT_DIR/rhods-operator" ]]; then
        print_step "rhods-operator directory already exists at $SCRIPT_DIR/rhods-operator, deleting for a fresh setup..."
        rm -rf "$SCRIPT_DIR/rhods-operator"
        print_success "Existing rhods-operator directory removed."
    fi

    # Clone RBC repository
    print_step "Cloning $RBC_REPO_LINK (branch: $BRANCH)..."
    git clone "$RBC_REPO_LINK" "$RBC_REPO_DIR" --branch "$BRANCH" --single-branch --depth=1 --quiet
    print_success "Repository cloned successfully"
    echo ""

    # Set up raw inputs directory
    print_header "Setting up raw inputs directory"
    echo ""
    print_step "Creating directory structure..."
    mkdir -p "$RAW_INPUTS_DIR"
    print_success "Directory created: $RAW_INPUTS_DIR"

    if [[ -d "$RBC_REPO_DIR/to-be-processed/bundle" ]]; then
        print_step "Copying bundle files to raw inputs directory..."
        cp -r "$RBC_REPO_DIR/to-be-processed/bundle/"* "$RAW_INPUTS_DIR"
        print_success "Bundle files copied successfully"
    else
        print_error "Source directory not found: $RBC_REPO_DIR/to-be-processed/bundle"
        exit 1
    fi
    echo ""

    # Set up Python virtual environment
    print_header "Setting up Python virtual environment"
    echo ""

    # Verify requirements file exists
    if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
        print_error "requirements.txt not found at $REQUIREMENTS_FILE"
        exit 1
    fi

    # Create virtual environment
    print_step "Creating Python virtual environment..."
    if [[ ! -d "$VENV_DIR" ]]; then
        python3 -m venv "$VENV_DIR"
        print_success "Virtual environment created: $VENV_DIR"
    else
        print_success "Virtual environment already exists: $VENV_DIR"
    fi

    # Install dependencies
    print_step "Installing Python dependencies from $REQUIREMENTS_FILE..."
    "$VENV_DIR/bin/pip" install --quiet --default-timeout=100 -r "$REQUIREMENTS_FILE"
    print_success "Python dependencies installed"
    echo ""

    # Initialize a new git repository in $SANDBOX_DIR to track changes
    print_header "Initializing git repository in $SANDBOX_DIR for diff viewing"
    if [[ -d "$RBC_REPO_DIR/.git" ]]; then
        print_step "Removing .git directory from $RBC_REPO_DIR..."
        rm -rf "$RBC_REPO_DIR/.git"
        print_success ".git directory removed from $RBC_REPO_DIR"
    fi
    cd "$SANDBOX_DIR"
    git init
    git add .
    git commit -m "Initial commit: raw bundle setup inputs" --quiet
    print_success "Git repository initialized - changes will be tracked"
else
    print_header "Running bundle-processor only!"
fi

print_header "Preparing bundle processing parameters"
print_step "Using bundle processor variables for branch: $BRANCH"

# Run bundle processor (always)
print_header "Running bundle-processor"

# Set up Quay token
QUAY_TOKEN_FILE="${HOME}/.ssh/.rhoai-quay-api-token"
if [[ -f "${QUAY_TOKEN_FILE}" ]]; then
    export RHOAI_QUAY_API_TOKEN=$(cat "${QUAY_TOKEN_FILE}" | tr -d '[:space:]')
else
    print_error "Quay token file not found: ${QUAY_TOKEN_FILE}"
    exit 1
fi

cd "${SANDBOX_DIR}"
"${VENV_DIR}/bin/python3" "${BUNDLE_PROCESSOR_SCRIPT}" \
    -op bundle-patch \
    -b "${BUILD_CONFIG_PATH}" \
    -c "${BUNDLE_CSV_PATH}" \
    -p "${PATCH_YAML_PATH}" \
    -sn "${SNAPSHOT_JSON_PATH}" \
    -o "${OUTPUT_FILE_PATH}" \
    -v "${BRANCH}" \
    -a "${ANNOTATION_YAML_PATH}" \
    --push-pipeline-yaml-path "${PUSH_PIPELINE_PATH}" \
    --push-pipeline-operation enable

if [[ $? -eq 0 ]]; then
    print_success "Bundle processing completed successfully"
else
    print_error "Bundle processing failed"
    exit 1
fi

echo "Copying processed bundle files back to sandbox directory..."
cp -r "${RAW_INPUTS_DIR}"/* "${SANDBOX_DIR}/${BRANCH}/bundle"
echo "Processed bundle files copied"
