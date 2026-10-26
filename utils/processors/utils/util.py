"""
Shared utility functions for RHOAI processors.
"""

import os
import re
import subprocess
import sys
import json
import tempfile
from typing import List, Dict, Tuple, Optional
import yaml
import ruamel.yaml as ruyaml
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString
import requests

from controller.quay_controller import quay_controller
from logger.logger import getLogger
import constants.constants as CONSTANTS

# Prevents ruamel.yaml from creating YAML anchor/alias references (&id001, *id001) when dumping files.
ruyaml.representer.RoundTripRepresenter.ignore_aliases = lambda self, data: True

LOGGER = getLogger('processor')


def to_plain_dict(obj):
    """
    Convert ruamel.yaml CommentedMap/CommentedSeq to regular Python dict/list.

    This is necessary because jsonupdate_ng doesn't work correctly with ruamel.yaml's
    special data types, causing entries to be lost during merges.

    Args:
        obj: A ruamel.yaml object (CommentedMap, CommentedSeq) or regular Python object

    Returns:
        Regular Python dict/list with the same data
    """
    return json.loads(json.dumps(obj, default=str))


def deduplicate_and_sort(
    entries: List[Dict],
    key: str = 'name',
    sort: bool = True
) -> List[Dict]:
    """
    Deduplicate a list of dictionaries by a specified key, keeping the first occurrence.
    Optionally sorts the result alphabetically by the same key.

    Args:
        entries: List of dictionaries to deduplicate
        key: The dictionary key to use for deduplication and sorting (default: 'name')
        sort: Whether to sort the result by the key (default: True)

    Returns:
        List of unique entries, optionally sorted by the key
    """
    if sort:
        LOGGER.info(f"Deduplicating and sorting by key: '{key}'")
    else:
        LOGGER.info(f"Deduplicating by key: '{key}'")

    seen = {}
    duplicates = []

    for entry in entries:
        entry_key = entry[key]
        if entry_key not in seen:
            seen[entry_key] = entry
        else:
            duplicates.append(entry_key)

    if duplicates:
        LOGGER.info(f"Duplicate entries removed: {duplicates}")
    else:
        LOGGER.info("No duplicate entries found")

    if sort:
        result = [seen[k] for k in sorted(seen.keys())]
    else:
        result = list(seen.values())

    LOGGER.debug(f"resulting list: {json.dumps(result, indent=2, default=str)}")
    return result


def parse_image_value(image_value: str) -> Dict[str, str]:
    """
    Parse an image value string into its components.

    Args:
        image_value: Full image reference (e.g., 'quay.io/rhoai/odh-dashboard-rhel8@sha256:abc123')

    Returns:
        Dict with keys: registry, org, repo, component_name, digest (if present)
    """
    # Split off digest if present
    if '@' in image_value:
        base, digest = image_value.split('@', 1)
    else:
        base, digest = image_value, None

    parts = base.split('/')
    registry = parts[0]
    org = parts[1]
    repo = '/'.join(parts[2:])

    # Derive component name by stripping RHEL suffix
    component_name = re.sub(r'-rhel\d+$', '', repo)

    return {
        'registry': registry,
        'org': org,
        'repo': repo,
        'component_name': component_name,
        'digest': digest
    }


def filter_image_entries(
    image_entries: List[Dict],
    include_filter: Optional[List[str]] = None,
    exclude_filter: Optional[List[str]] = None
) -> List[Dict]:
    """
    Filter image entries based on include/exclude patterns.

    Note: Only one filter type can be used at a time. If both are provided, an error is raised.

    Args:
        image_entries: List of image entry dicts with 'name' and 'value' keys
        include_filter: If provided, only include entries where name matches the pattern
        exclude_filter: If provided, exclude entries where name matches the pattern

    Returns:
        Filtered list of image entries

    Raises:
        ValueError: If both include_filter and exclude_filter are provided
    """
    if include_filter and exclude_filter:
        LOGGER.error("Cannot use both include_filter and exclude_filter at the same time")
        raise ValueError("Cannot use both include_filter and exclude_filter at the same time")

    result = []

    if include_filter:
        LOGGER.info(f"Filtering image entries to INCLUDE patterns: {include_filter}")
        LOGGER.info("Following components will be added:")
        for entry in image_entries:
            for pattern in include_filter:
                if pattern in entry['name']:
                    LOGGER.info(f"  + {entry['name']} (matched pattern: '{pattern}')")
                    result.append(entry)
                    break
    elif exclude_filter:
        LOGGER.info(f"Filtering image entries to EXCLUDE patterns: {exclude_filter}")
        LOGGER.info("Following components will be removed:")
        for entry in image_entries:
            matched = False
            for pattern in exclude_filter:
                if pattern in entry['name']:
                    LOGGER.info(f"  - {entry['name']} (matched pattern: '{pattern}')")
                    matched = True
                    break
            if not matched:
                result.append(entry)
    else:
        # No filter applied, return all entries
        result = list(image_entries)

    LOGGER.debug(f"Filtered image entries: {json.dumps(result, indent=4, default=str)}")
    return result


def fetch_latest_images_and_git_metadata(
    image_entries: List[Dict],
    image_tag: str,
    use_github_override: bool = False
) -> Tuple[List[Dict], Dict]:
    """
    For each image entry in the provided list, query Quay.io for the latest image digest and git metadata
    corresponding to the given image_tag.

    If any images are missing the  image_tag, or git labels, the function will exit with an error.

    Args:
        image_entries: List of image entry dicts with 'name' and 'value' keys
        image_tag: Image tag to search for (e.g., 'rhoai-3.2' or 'rhoai-3.2-nightly')
        use_github_override: If True, prefer github.url/github.commit labels over git.url/git.commit

    Returns:
        Tuple of (latest_images, git_labels_meta)
        - latest_images: List of image entries with updated digest values
        - git_labels_meta: Dict with 'map' key containing component git metadata
    """
    latest_images = []
    git_labels_meta = {'map': {}, 'errors': {}}
    missing_images = []
    missing_signatures = []
    missing_git_labels = []

    total_images = len(image_entries)
    for image_number, image_entry in enumerate(image_entries, start=1):
        image_value = image_entry['value']
        LOGGER.info(f'  [{image_number}/{total_images}] Processing: {image_value.split("@")[0]}')
        LOGGER.debug(f'Full image entry: {json.dumps(image_entry)}')

        # Parse image value
        parsed = parse_image_value(image_value)
        registry = parsed['registry']
        org = parsed['org']
        repo = parsed['repo']
        component_name = parsed['component_name']

        LOGGER.debug(f"registry: {registry}, org: {org}, repo: {repo}, component_name: {component_name}")

        # Create quay controller for this org
        qc = quay_controller(org)

        # Get all tags for the image_tag
        LOGGER.debug(f"Fetching all tags for image_tag: {image_tag}")
        tags = qc.get_all_tags(repo, image_tag)
        LOGGER.debug(f"Fetched tags: {tags}")

        if not tags:
            LOGGER.warning(f"'{image_tag}' tag not found for image: '{repo}'")
            missing_images.append(repo)
            git_labels_meta['errors'][component_name] = 'missing_image'
            continue

        signed_tag = None
        for tag in tags:
            sig_tag = f'{tag["manifest_digest"].replace(":", "-")}.sig'
            signature = qc.get_tag_details(repo, sig_tag)

            if signature:
                signed_tag = tag
                break

        if not signed_tag:
            LOGGER.warning(f"  No cosign signature found for '{component_name}'. Image exists but is not signed.")
            missing_signatures.append(repo)
            git_labels_meta['errors'][component_name] = 'missing_signature'
            continue

        tag = signed_tag
        value = f'{registry}/{org}/{repo}@{tag["manifest_digest"]}'
        # Create a copy to avoid mutating the original entry in operands_map_dict.
        # Shared references cause jsonupdate_ng to produce incorrect merge results.
        updated_entry = dict(image_entry)
        updated_entry['value'] = DoubleQuotedScalarString(value)
        latest_images.append(updated_entry)

        manifest_digest = tag["manifest_digest"]
        LOGGER.debug(f'manifest_digest = {manifest_digest}')

        # The manifest list does not get the image labels, so we need to fetch the manifest digest to retrieve the git labels
        if tag['is_manifest_list']:
            LOGGER.debug('Manifest list detected. Fetching manifest digest to retrieve git labels...')
            image_manifest_digests = qc.get_image_manifest_digests_for_all_the_supported_archs(repo, manifest_digest)
            if image_manifest_digests:
                manifest_digest = image_manifest_digests[0]
                LOGGER.debug(f'Manifest_digest used to retrieve git labels: {manifest_digest}')

        # Get git labels
        labels = qc.get_git_labels(repo, manifest_digest)
        labels = {label['key']:label['value'] for label in labels if label['value']}

        # Extract git URL and commit with safe defaults
        git_url = labels.get(CONSTANTS.GIT_URL_LABEL_KEY, '')
        git_commit = labels.get(CONSTANTS.GIT_COMMIT_LABEL_KEY, '')

        # Apply GitHub overrides if enabled and available
        if use_github_override:
            if CONSTANTS.GITHUB_URL_LABEL_KEY in labels:
                git_url = labels[CONSTANTS.GITHUB_URL_LABEL_KEY]
                LOGGER.debug("Using github.url override")
            if CONSTANTS.GITHUB_COMMIT_LABEL_KEY in labels:
                git_commit = labels[CONSTANTS.GITHUB_COMMIT_LABEL_KEY]
                LOGGER.debug("Using github.commit override")

        # Store git metadata
        git_labels_meta['map'][component_name] = {}
        git_labels_meta['map'][component_name][CONSTANTS.GIT_URL_LABEL_KEY] = git_url
        git_labels_meta['map'][component_name][CONSTANTS.GIT_COMMIT_LABEL_KEY] = git_commit
        LOGGER.debug(f"Collected git labels for '{component_name}': git.url='{git_url}', git.commit='{git_commit}'")

        # Collect repo with missing git_url or git_commit
        if not git_url or not git_commit:
            missing_git_labels.append(repo)
            git_labels_meta['errors'][component_name] = 'missing_git_labels'

    # Exit on missing images
    if missing_images:
        LOGGER.error(f'Images missing for following components: {missing_images}')
        LOGGER.error(f"Please verify that these images exist in the Quay.io repository with the image tag: '{image_tag}'")
        sys.exit(1)

    # Exit on missing cosign signatures
    if missing_signatures:
        LOGGER.error(f'Cosign signature missing for following components: {missing_signatures}')
        LOGGER.error(f"Please sign these images with cosign so the processor can verify and extract labels.")
        sys.exit(1)

    # Exit on missing git labels
    if missing_git_labels:
        LOGGER.error(f'git.url and/or git.commit labels missing/empty for following components: {missing_git_labels}')
        LOGGER.error(f"Please rebuild these images with git.url and git.commit labels in their OCI metadata.")
        sys.exit(1)

    LOGGER.info("Processing completed successfully. Returning latest images and git labels metadata.")
    LOGGER.info(f'latest_images: {json.dumps(latest_images, indent=4, default=str)}')
    LOGGER.info(f'git_labels_meta: {json.dumps(git_labels_meta, indent=4, default=str)}')

    return latest_images, git_labels_meta



_DIGEST_RE = re.compile(r'^sha256:[a-f0-9]{64}$')


def classify_unresolvable_image(qc, parsed: Dict, rhoai_version: Optional[str] = None) -> str:
    """
    Explain why an image digest could not be resolved against Quay, so the skip
    warning states the real cause instead of a catch-all "cross-tenant" guess.

    This is a best-effort explanation only — it must NEVER raise, since its whole
    purpose is to let the freeze continue past an unresolvable image.

    Order of checks (cheapest first):
      1. Malformed digest — missing or duplicated 'sha256:' prefix in the source
         file (a data bug, not a registry limitation).
      2. Tag present, different digest — the source file digest is stale; the
         freeze (tag reconciliation) will fix it on a real run.
      3. Otherwise (tag resolves to the same digest we already failed to query,
         or the tag/repo itself is not queryable) — a genuinely cross-tenant /
         synced image, or an orphaned digest. Metadata is maintained out of band.
    """
    digest = parsed.get('digest') or ''
    if not _DIGEST_RE.match(digest):
        return (f"malformed digest '{digest}' (missing or duplicated 'sha256:' prefix) "
                f"in the source file — fix the reference")

    generic = "manifest/labels not queryable (cross-tenant/synced image or orphaned digest)"
    if not rhoai_version:
        return generic

    try:
        tag = qc.get_tag_details(parsed['repo'], rhoai_version)
    except Exception as exc:  # never let classification crash the freeze
        LOGGER.warning(f"  Tag lookup failed for {parsed['org']}/{parsed['repo']}:{rhoai_version}: {exc}")
        return generic

    tag_digest = tag.get('manifest_digest', '') if tag else ''
    if tag_digest and tag_digest != digest:
        return (f"stale digest in source file; current '{rhoai_version}' tag is {tag_digest} — "
                f"run the freeze to reconcile")

    return generic


def fetch_git_metadata_for_existing_digests(image_entries: List[Dict], rhoai_version: Optional[str] = None) -> Dict:
    """
    Fetch git labels from Quay using the existing digest in each image entry.
    No tag lookup is performed — the manifest digest in each image reference
    is used directly.  Callers are responsible for any pre-filtering.

    Images whose manifest/labels cannot be resolved (cross-tenant synced images,
    malformed or stale digests) are recorded under 'skipped' with a human-readable
    reason instead of aborting; the caller must preserve their existing metadata.
    """
    git_labels_meta = {'map': {}, 'skipped': {}}

    for image_entry in image_entries:
        image_value = str(image_entry['value'])
        parsed = parse_image_value(image_value)
        LOGGER.info(f'  Processing (existing digest): {parsed["org"]}/{parsed["repo"]}')

        qc = quay_controller(parsed['org'])
        manifest_digest = parsed['digest']

        manifest_details = qc.get_manifest_details(parsed['repo'], manifest_digest, fail_on_error=False)
        if manifest_details is None:
            reason = classify_unresolvable_image(qc, parsed, rhoai_version)
            LOGGER.warning(f'  Skipping {parsed["component_name"]}: {reason}. Keeping existing git metadata.')
            git_labels_meta['skipped'][parsed['component_name']] = reason
            continue
        if manifest_details.get('is_manifest_list'):
            arch_digests = qc.get_image_manifest_digests_for_all_the_supported_archs(parsed['repo'], manifest_digest)
            if arch_digests:
                manifest_digest = arch_digests[0]

        labels = qc.get_git_labels(parsed['repo'], manifest_digest, fail_on_error=False)
        if labels is None:
            reason = classify_unresolvable_image(qc, parsed, rhoai_version)
            LOGGER.warning(f'  Skipping {parsed["component_name"]}: {reason}. Keeping existing git metadata.')
            git_labels_meta['skipped'][parsed['component_name']] = reason
            continue
        labels = {l['key']: l['value'] for l in labels if l['value']}

        git_labels_meta['map'][parsed['component_name']] = {
            CONSTANTS.GIT_URL_LABEL_KEY: labels.get(CONSTANTS.GIT_URL_LABEL_KEY, ''),
            CONSTANTS.GIT_COMMIT_LABEL_KEY: labels.get(CONSTANTS.GIT_COMMIT_LABEL_KEY, ''),
        }

    LOGGER.info(f'git_labels_meta: {json.dumps(git_labels_meta, indent=4)}')
    return git_labels_meta


def fetch_files_from_git_repo(
    git_url: str,
    git_commit: str,
    file_paths: List[str]
) -> Dict[str, str]:
    """
    Fetch files from a git repo by cloning at a specific commit.
    Works with any git host (GitHub, GitLab, etc.).

    Returns:
        Dict mapping file_path -> file content string
    """
    with tempfile.TemporaryDirectory() as clone_dir:
        LOGGER.info(f"  Cloning {git_url} at {git_commit}...")
        subprocess.run(
            ["git", "clone", "--depth=1", "--filter=blob:none", "--no-checkout",
             git_url, clone_dir],
            check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "-C", clone_dir, "sparse-checkout", "set", "--no-cone"] + file_paths,
            check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "-C", clone_dir, "fetch", "origin", git_commit],
            check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "-C", clone_dir, "checkout", git_commit],
            check=True, capture_output=True, text=True
        )

        results = {}
        for fp in file_paths:
            full_path = os.path.join(clone_dir, fp)
            with open(full_path) as f:
                results[fp] = f.read()
            LOGGER.info(f"  Fetched {fp} ({len(results[fp])} bytes)")

        return results


def process_push_pipeline(
    push_pipeline_dict: Dict,
    operation: str
) -> Tuple[bool, Dict]:
    """
    Enables or disables the Tekton push (CI) pipeline trigger based on the build context.

    Context:
        There are two pipelines:
            - One for nightly builds (odh-operator-vx-y-scheduled.yaml)
            - One for CI "push" builds (odh-operator-vx-y-push.yaml)

        When building for nightly, we want to ensure the CI push pipeline does NOT trigger.

    Implementation:
        The pipeline trigger is controlled with a CEL expression under the
        'pipelinesascode.tekton.dev/on-cel-expression' annotation.

        To disable the push pipeline (for nightly), this function prepends:
            '"non-existent-file.non-existent-ext".pathChanged() && '
        to the CEL expression. This always-false check ensures the pipeline is never triggered.

        To enable the push pipeline (for CI builds), this function removes the above
        always-false condition if present.

    Args:
        push_pipeline_dict: The push pipeline YAML dict
        operation: 'enable' or 'disable'

    Returns:
        Tuple of (is_updated, push_pipeline_dict)
        - is_updated: True if the pipeline dict was modified, False if no change was needed
        - push_pipeline_dict: The (potentially modified) pipeline dict to be written
    """
    current_on_cel_expr = push_pipeline_dict['metadata']['annotations']['pipelinesascode.tekton.dev/on-cel-expression']
    disable_ext = 'non-existent-file.non-existent-ext'
    disable_expr = f'"{disable_ext}".pathChanged() && '
    is_updated = False

    LOGGER.info(f"Push pipeline operation requested: {operation}")
    LOGGER.debug(f"Current CEL expression before update: {current_on_cel_expr}")

    if operation.lower() == 'enable' and disable_ext in current_on_cel_expr:
        # Enable: remove the always-false CEL condition
        LOGGER.info("Enabling push pipeline trigger (removing always-false CEL condition).")
        push_pipeline_dict['metadata']['annotations']['pipelinesascode.tekton.dev/on-cel-expression'] = current_on_cel_expr.replace(disable_expr, '')
        is_updated = True

    elif operation.lower() == 'disable' and disable_ext not in current_on_cel_expr:
        # Disable: inject the always-false CEL condition
        LOGGER.info("Disabling push pipeline trigger (injecting always-false CEL condition).")
        push_pipeline_dict['metadata']['annotations']['pipelinesascode.tekton.dev/on-cel-expression'] = f'{disable_expr}{current_on_cel_expr}'
        is_updated = True

    if is_updated:
        LOGGER.debug(f"CEL expression after update: {push_pipeline_dict['metadata']['annotations']['pipelinesascode.tekton.dev/on-cel-expression']}")
    else:
        LOGGER.info(f"No change needed for push pipeline CEL expression. The push pipeline is already {operation}d.")

    return is_updated, push_pipeline_dict


def load_yaml_file(file_path: str, parser: str = 'ruamel') -> Dict:
    """
    Load a YAML file and return its contents as a dictionary.

    Args:
        file_path: The path to the YAML file to load
        parser: Which parser to use:
                - 'ruamel': Uses ruamel.yaml RoundTripLoader to preserve quotes and formatting (default)
                - 'pyyaml': Uses standard yaml.safe_load for simple loading

    Returns:
        The parsed YAML content as a dictionary
    """
    LOGGER.info(f"  Parsing yaml file: {file_path}")
    with open(file_path, 'r') as f:
        if parser == 'ruamel':
            LOGGER.debug("  Using ruamel.yaml parser")
            return ruyaml.load(f, Loader=ruyaml.RoundTripLoader, preserve_quotes=True)
        elif parser == 'pyyaml':
            LOGGER.debug("  Using pyyaml parser")
            return yaml.safe_load(f)
        else:
            raise ValueError(f"Unknown parser '{parser}'. Use 'ruamel' or 'pyyaml'.")


def load_multi_document_yaml_file(file_path: str) -> List[Dict]:
    """
    Load a multi-document YAML file with full round-trip preservation
    and return all documents as a list.

    Args:
        file_path: Path to the YAML file.

    Returns:
        List of parsed YAML documents.
    """
    LOGGER.info(f"  Parsing multi-doc yaml file: {file_path}")
    yaml_instance = YAML()
    yaml_instance.preserve_quotes = True
    with open(file_path, 'r') as f:
        docs = list(yaml_instance.load_all(f))

    LOGGER.debug(f"  Loaded {len(docs)} documents from {file_path}")
    return docs


def write_yaml_file(data: Dict, file_path: str) -> None:
    """
    Write a dictionary to a YAML file using ruamel.yaml with RoundTripDumper.

    This preserves the formatting, quotes, and comments in the YAML output.

    Args:
        data: The dictionary to write to the YAML file
        file_path: The path to the output file
    """
    LOGGER.info(f"  Writing YAML file: {file_path}")
    with open(file_path, 'w') as f:
        ruyaml.dump(data, f, Dumper=ruyaml.RoundTripDumper, default_flow_style=False)


# Round-trip YAML functions using the newer ruamel.yaml YAML() API.
# The existing load_yaml_file / write_yaml_file use the legacy RoundTripLoader / RoundTripDumper
# which work well for bundle processing but drop comments, blank lines, and alter list indentation.
# Rather than modifying the existing functions and risking regressions in the bundle processor,
# these _rt variants were created for newer use cases (e.g. Helm charts).

def load_yaml_file_rt(file_path: str) -> Dict:
    """
    Load a YAML file with full round-trip preservation (comments, blank lines, indentation).

    Args:
        file_path: The path to the YAML file to load

    Returns:
        The parsed YAML content as a dictionary (CommentedMap)
    """
    LOGGER.info(f"  Parsing yaml file (round-trip): {file_path}")
    yaml_instance = YAML()
    yaml_instance.preserve_quotes = True
    with open(file_path, 'r') as f:
        return yaml_instance.load(f)


def write_yaml_file_rt(data: Dict, file_path: str,
                        indent_mapping: int = 2,
                        indent_sequence: int = 4,
                        indent_offset: int = 2) -> None:
    """
    Write a dictionary to a YAML file with full round-trip preservation.

    Args:
        data: The dictionary to write to the YAML file
        file_path: The path to the output file
        indent_mapping: Spaces per mapping indent level (default: 2)
        indent_sequence: Spaces per sequence indent level (default: 4)
        indent_offset: Offset of the sequence dash within the indent (default: 2)
    """
    LOGGER.info(f"  Writing YAML file (round-trip): {file_path}")
    yaml_instance = YAML()
    yaml_instance.preserve_quotes = True
    yaml_instance.representer.ignore_aliases = lambda data: True
    yaml_instance.indent(mapping=indent_mapping, sequence=indent_sequence, offset=indent_offset)
    with open(file_path, 'w') as f:
        yaml_instance.dump(data, f)


def read_file(file_path: str) -> str:
    """
    Read a text file and return its contents as a string.

    Args:
        file_path: The path to the file to read

    Returns:
        The file contents as a string
    """
    LOGGER.info(f"  Reading file: {file_path}")
    with open(file_path, 'r') as f:
        return f.read()


def read_file_lines(file_path: str) -> List[str]:
    """
    Read a text file and return its contents as a list of lines.

    Args:
        file_path: The path to the file to read

    Returns:
        List of lines from the file
    """
    LOGGER.info(f"  Reading file lines: {file_path}")
    with open(file_path, 'r') as f:
        return f.readlines()


def write_file(content: str, file_path: str) -> None:
    """
    Write string content to a file.

    Args:
        content: The string content to write
        file_path: The path to the output file
    """
    LOGGER.info(f"  Writing file: {file_path}")
    with open(file_path, 'w') as f:
        f.write(content)


def fetch_file_data_from_github(
    git_url: str,
    git_commit: str,
    file_path: str,
    timeout: int = 30
) -> str:
    """
    Fetch a file directly from GitHub using the raw content URL.

    This function converts a GitHub repository URL and commit SHA into a raw content URL
    and downloads the file content.

    Args:
        git_url: GitHub repository URL (e.g., 'https://github.com/org/repo' or
                 'https://github.com/org/repo.git')
        git_commit: Commit SHA or branch/tag name
        file_path: Path to the file within the repository (e.g., 'build/operands-map.yaml')
        timeout: Request timeout in seconds (default: 30)

    Returns:
        Raw file content as string

    Raises:
        requests.HTTPError: If the HTTP request fails (e.g., 404 Not Found)
    """
    # Normalize git_url: remove trailing .git if present
    if git_url.endswith('.git'):
        git_url = git_url[:-4]

    # Convert GitHub URL to raw content URL
    # https://github.com/org/repo -> https://raw.githubusercontent.com/org/repo
    raw_base_url = git_url.replace('github.com', 'raw.githubusercontent.com')
    raw_url = f"{raw_base_url}/{git_commit}/{file_path}"

    LOGGER.info(f"Fetching content from {raw_url}")

    response = requests.get(raw_url, timeout=timeout)
    response.raise_for_status()

    content = response.text
    LOGGER.info(f"Successfully fetched {len(content)} bytes")

    return content


def apply_registry_and_repo_replacements(
    image_entries: List[Dict],
    registry_mapping: Dict[str, str],
    repo_mappings: Dict[str, str]
) -> None:
    """
    Replaces build registry/repo paths with release registry/repo paths in image entries.

    This function modifies the image entries in-place, replacing registry and repo paths
    based on the provided mappings.

    Args:
        image_entries: List of image entry dicts with 'name' and 'value' keys.
                       Modified in-place.
        registry_mapping: Dict mapping build registry to release registry

        repo_mappings: Dict mapping build repo paths to release repo paths

    Example:
        image_entries = [
            {'name': 'LLAMA_STACK', 'value': 'quay.io/aipcc/llama-stack-core@sha256:abc'},
            {'name': 'DASHBOARD', 'value': 'quay.io/modh/odh-dashboard@sha256:def'},
            {'name': 'OPERATOR', 'value': 'quay.io/rhoai/odh-rhel9-operator@sha256:ghi'},
        ]

        registry_mapping = {
            'quay.io': 'registry.redhat.io',
        }

        repo_mappings = {
            'aipcc/llama-stack-core': 'rhoai/odh-llama-stack-core',
            'modh/odh-dashboard': 'rhoai/odh-dashboard',
            'rhoai/odh-rhel9-operator': 'rhoai/odh-rhel9-operator',
        }

        After apply_registry_and_repo_replacements(image_entries, registry_mapping, repo_mappings):

        image_entries = [
            {'name': 'LLAMA_STACK', 'value': 'registry.redhat.io/rhoai/odh-llama-stack-core@sha256:abc'},
            {'name': 'DASHBOARD', 'value': 'registry.redhat.io/rhoai/odh-dashboard@sha256:def'},
            {'name': 'OPERATOR', 'value': 'registry.redhat.io/rhoai/odh-rhel9-operator@sha256:ghi'},
        ]
    """
    LOGGER.info(f"Applying registry and repo replacements to {len(image_entries)} image(s)")

    # Validate and extract the single registry mapping entry
    if len(registry_mapping) != 1:
        raise ValueError(f"registry_mapping must have exactly one entry, got {len(registry_mapping)}")
    source_registry, target_registry = next(iter(registry_mapping.items()))

    for image_entry in image_entries:
        original_value = image_entry.get('value', '')
        new_value = original_value

        if new_value:
            for source_repo, target_repo in repo_mappings.items():
                new_value = new_value.replace(
                    f'{source_registry}/{source_repo}@',
                    f'{target_registry}/{target_repo}@'
                )

        image_entry['value'] = DoubleQuotedScalarString(new_value)
        LOGGER.debug(f"  {original_value} -> {new_value}")
