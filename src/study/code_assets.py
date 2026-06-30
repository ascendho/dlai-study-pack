import base64
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import List
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup


SKIP_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".ipynb_checkpoints",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
}

SKIP_FILE_NAMES = {
    ".DS_Store",
}

CODE_GROUP_LESSONS = "lessons"
CODE_GROUP_PROJECT = "project"
CODE_GROUPS = {CODE_GROUP_LESSONS, CODE_GROUP_PROJECT}
_SHARED_CODE_MARKER = "_DLAI_SHARED_CODE_ROOT"


class CodeDownloadError(RuntimeError):
    pass


@dataclass
class CodeAssetFile:
    path: str
    status: str
    bytes: int = 0
    message: str = ""


@dataclass
class CodeAssetSummary:
    source_url: str
    output_dir: Path
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    deduplicated: int = 0
    rewritten: int = 0
    files: List[CodeAssetFile] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class JupyterLabLink:
    url: str
    token: str = ""
    lesson_url: str = ""
    group: str = CODE_GROUP_LESSONS


@dataclass
class ContentsApiLocation:
    base_url: str
    root_path: str = ""
    query: str = ""

    def url_for(self, contents_path, token=None):
        path = _clean_remote_path(contents_path)
        base_path = urlparse(self.base_url).path.rstrip("/")
        full_path = base_path
        if path:
            full_path = "{}/{}".format(full_path, _quote_path(path))

        parsed = urlparse(self.base_url)
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                full_path,
                "",
                _query_with_content(self.query, token=token),
                "",
            )
        )

    def tree_url_for(self, tree_path=None, token=None):
        parsed = urlparse(self.base_url)
        prefix = parsed.path.rstrip("/")
        marker = "/api/contents"
        if prefix.endswith(marker):
            prefix = prefix[: -len(marker)]
        path = "{}/tree".format(prefix.rstrip("/")) if prefix else "/tree"
        remote_path = _clean_remote_path(self.root_path if tree_path is None else tree_path)
        if remote_path:
            path = "{}/{}".format(path.rstrip("/"), _quote_path(remote_path))
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                path,
                "",
                _query_without_content(self.query, token=token),
                "",
            )
        )


class BrowserJupyterClient:
    def __init__(self, browser_fetcher):
        self.browser_fetcher = browser_fetcher

    def prime(self, url):
        self.browser_fetcher.fetch_page(url)

    def get_json(self, url):
        return self.browser_fetcher.fetch_json(url)

    def authenticate(self, login_url, check_url):
        return self.browser_fetcher.authenticate_jupyter(login_url, check_url)


class JupyterCodeDownloader:
    def __init__(self, client, output_root, course_slug, force=False, code_token=""):
        self.client = client
        self.output_dir = Path(output_root) / course_slug / "code"
        self.force = force
        self.code_token = code_token

    def download(self, code_url, discovered_links=None):
        summary = CodeAssetSummary(source_url=redact_url(code_url), output_dir=self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        for source_url, token, group in self._download_sources(code_url, discovered_links or []):
            try:
                location = parse_jupyter_contents_location(source_url)
                root = self._fetch_root(location, token)
                self._walk_entry(root, location, summary, token, group)
            except Exception as exc:
                summary.failed += 1
                summary.errors.append(redact_url(str(exc)))

        if summary.saved or summary.skipped:
            _deduplicate_code_groups(summary)
        return summary

    def _download_sources(self, code_url, discovered_links):
        sources = []
        host_tokens = _host_tokens(discovered_links)
        if self.code_token:
            sources.extend(
                (link.url, self.code_token, _normalize_code_group(link.group))
                for link in discovered_links
            )
            sources.append((code_url, self.code_token, CODE_GROUP_LESSONS))
        else:
            sources.extend(
                (link.url, link.token or None, _normalize_code_group(link.group))
                for link in discovered_links
            )
            manual_token = _token_from_url(code_url) or host_tokens.get(urlparse(code_url).netloc)
            if manual_token or not discovered_links:
                sources.append((code_url, manual_token, CODE_GROUP_LESSONS))

        seen = set()
        seen_identities = set()
        unique_sources = []
        for source_url, token, group in sources:
            if not source_url:
                continue
            identity = _source_identity(source_url)
            if group == CODE_GROUP_LESSONS and identity in seen_identities:
                continue
            key = (identity, group)
            if key in seen:
                continue
            seen.add(key)
            seen_identities.add(identity)
            unique_sources.append((source_url, token, group))
        return unique_sources

    def _fetch_root(self, location, token):
        api_url = location.url_for(location.root_path, token=token)
        try:
            return self.client.get_json(api_url)
        except Exception as exc:
            if not _is_jupyter_auth_error(exc):
                raise
            login_url = location.tree_url_for(token=token)
            if token and hasattr(self.client, "prime"):
                try:
                    self.client.prime(login_url)
                    return self.client.get_json(api_url)
                except Exception as prime_exc:
                    if not _is_jupyter_auth_error(prime_exc):
                        raise
            if not hasattr(self.client, "authenticate"):
                raise CodeDownloadError(_jupyter_auth_message(location)) from exc

            try:
                payload = self.client.authenticate(login_url, api_url)
                if payload is not None:
                    return payload
            except Exception as auth_exc:
                raise CodeDownloadError("{} {}".format(_jupyter_auth_message(location), auth_exc)) from exc

            return self.client.get_json(api_url)

    def _walk_entry(self, entry, location, summary, token, group):
        entry_type = entry.get("type", "")
        if entry_type == "directory":
            self._walk_directory(entry, location, summary, token, group)
            return
        if entry_type in {"file", "notebook"}:
            self._save_file(entry, location, summary, token, group)
            return

        path = _relative_remote_path(
            entry.get("path", ""),
            location.root_path,
            entry.get("name", ""),
        )
        summary.skipped += 1
        summary.files.append(
            CodeAssetFile(
                path=_asset_path(group, path),
                status="skipped",
                message="unsupported Jupyter content type: {}".format(entry_type or "unknown"),
            )
        )

    def _walk_directory(self, entry, location, summary, token, group):
        name = entry.get("name", "")
        path = _clean_remote_path(entry.get("path", name))
        if _should_skip_directory(path, name):
            return

        content = entry.get("content")
        if content is None:
            entry = self.client.get_json(location.url_for(path, token=token))
            content = entry.get("content")
        if content is None:
            raise CodeDownloadError("directory listing is missing content: {}".format(path or "/"))

        for child in content:
            self._walk_entry(child, location, summary, token, group)

    def _save_file(self, entry, location, summary, token, group):
        name = entry.get("name", "")
        path = _clean_remote_path(entry.get("path", name))
        relative_path = _relative_remote_path(path, location.root_path, name)
        asset_path = _asset_path(group, relative_path)

        if _should_skip_file(relative_path, name):
            return

        output_path = _safe_output_path(self.output_dir / _normalize_code_group(group), relative_path)
        if output_path is None:
            summary.skipped += 1
            summary.files.append(
                CodeAssetFile(
                    path=asset_path,
                    status="skipped",
                    message="unsafe path",
                )
            )
            return

        if output_path.exists() and not self.force:
            summary.skipped += 1
            summary.files.append(CodeAssetFile(path=asset_path, status="skipped"))
            return

        try:
            if "content" not in entry or entry.get("content") is None:
                entry = self.client.get_json(location.url_for(path, token=token))
            data = _entry_bytes(entry)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(data)
        except Exception as exc:
            summary.failed += 1
            summary.files.append(
                CodeAssetFile(
                    path=asset_path,
                    status="failed",
                    message=str(exc),
                )
            )
            return

        summary.saved += 1
        summary.files.append(
            CodeAssetFile(
                path=asset_path,
                status="saved",
                bytes=len(data),
            )
        )


def _deduplicate_code_groups(summary):
    for group in CODE_GROUPS:
        group_dir = summary.output_dir / group
        if not group_dir.is_dir():
            continue
        _deduplicate_group(summary, group, group_dir)


def _deduplicate_group(summary, group, group_dir):
    directories_by_name = {}
    for directory in sorted(group_dir.rglob("*")):
        if not directory.is_dir():
            continue
        directories_by_name.setdefault(directory.name, []).append(directory)

    for name, directories in sorted(directories_by_name.items()):
        if len(directories) < 2:
            continue
        signatures = {}
        for directory in directories:
            signature = _directory_signature(directory)
            if signature:
                signatures.setdefault(signature, []).append(directory)

        for matching_directories in signatures.values():
            matching_directories = [directory for directory in matching_directories if directory.exists()]
            if len(matching_directories) < 2:
                continue
            _deduplicate_matching_directories(summary, group, group_dir, name, matching_directories)


def _deduplicate_matching_directories(summary, group, group_dir, name, directories):
    directories = sorted(directories, key=lambda path: path.relative_to(group_dir).as_posix())
    root_candidate = group_dir / name
    promoted_from = None

    if root_candidate in directories:
        canonical_dir = root_candidate
    else:
        promoted_from = directories[0]
        canonical_dir = root_candidate
        if canonical_dir.exists():
            return
        canonical_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(promoted_from), str(canonical_dir))
        _promote_summary_entries(summary, group, promoted_from, canonical_dir, group_dir)

    removed_dirs = [directory for directory in directories if directory != canonical_dir]
    if promoted_from is not None:
        removed_dirs = [directory for directory in removed_dirs if directory != promoted_from]
        removed_dirs.insert(0, promoted_from)

    for duplicate_dir in removed_dirs:
        _rewrite_references_for_removed_directory(summary, duplicate_dir, canonical_dir, group_dir)
        if duplicate_dir.exists():
            _mark_deduplicated_entries(summary, group, duplicate_dir, canonical_dir, group_dir)
            shutil.rmtree(duplicate_dir)

    summary.rewritten += _rewrite_canonical_self_references(canonical_dir, name)


def _directory_signature(directory):
    signature = []
    for child in sorted(directory.rglob("*")):
        relative_path = child.relative_to(directory).as_posix()
        if child.is_dir():
            signature.append(("dir", relative_path, b""))
            continue
        if child.is_file():
            signature.append(("file", relative_path, child.read_bytes()))
    return tuple(signature)


def _promote_summary_entries(summary, group, old_dir, new_dir, group_dir):
    old_prefix = _summary_prefix(group, old_dir, group_dir)
    new_prefix = _summary_prefix(group, new_dir, group_dir)
    for file in summary.files:
        if file.path == old_prefix or file.path.startswith(old_prefix + "/"):
            file.path = new_prefix + file.path[len(old_prefix) :]
            if file.message:
                file.message = file.message.replace(old_prefix, new_prefix)


def _mark_deduplicated_entries(summary, group, duplicate_dir, canonical_dir, group_dir):
    duplicate_prefix = _summary_prefix(group, duplicate_dir, group_dir)
    canonical_prefix = _summary_prefix(group, canonical_dir, group_dir)
    seen = set()

    for file in summary.files:
        if not (file.path == duplicate_prefix or file.path.startswith(duplicate_prefix + "/")):
            continue
        seen.add(file.path)
        if file.status == "saved":
            summary.saved -= 1
        elif file.status == "skipped":
            summary.skipped -= 1
        file.status = "deduplicated"
        file.bytes = 0
        file.message = "deduplicated into {}{}".format(
            canonical_prefix,
            file.path[len(duplicate_prefix) :],
        )
        summary.deduplicated += 1

    for child in sorted(duplicate_dir.rglob("*")):
        if not child.is_file():
            continue
        asset_path = "{}/{}".format(group, child.relative_to(group_dir).as_posix())
        if asset_path in seen:
            continue
        target = "{}{}".format(canonical_prefix, asset_path[len(duplicate_prefix) :])
        summary.files.append(CodeAssetFile(asset_path, "deduplicated", message="deduplicated into {}".format(target)))
        summary.deduplicated += 1


def _rewrite_references_for_removed_directory(summary, removed_dir, canonical_dir, group_dir):
    affected_root = removed_dir.parent
    folder_name = canonical_dir.name
    rewritten = 0
    for path in sorted(affected_root.rglob("*")):
        if not path.is_file() or _is_relative_to(path, removed_dir):
            continue
        if path.suffix == ".py":
            rewritten += _rewrite_python_imports(path, group_dir, folder_name)
        elif path.suffix == ".ipynb":
            rewritten += _rewrite_notebook_imports(path, group_dir, folder_name)
    summary.rewritten += rewritten


def _rewrite_python_imports(path, group_dir, folder_name):
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return 0
    if _SHARED_CODE_MARKER in text or not _imports_folder(text, folder_name):
        return 0

    bootstrap = _python_shared_path_bootstrap(_python_code_root_expression(path, group_dir))
    lines = text.splitlines(keepends=True)
    insert_at = _first_folder_import_line(lines, folder_name)
    if insert_at is None:
        return 0
    lines.insert(insert_at, bootstrap)
    path.write_text("".join(lines), encoding="utf-8")
    return 1


def _rewrite_notebook_imports(path, group_dir, folder_name):
    try:
        notebook = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0
    cells = notebook.get("cells")
    if not isinstance(cells, list):
        return 0
    if any(_SHARED_CODE_MARKER in _cell_source_text(cell) for cell in cells):
        return 0

    for index, cell in enumerate(cells):
        if cell.get("cell_type") != "code":
            continue
        if not _imports_folder(_cell_source_text(cell), folder_name):
            continue
        cells.insert(
            index,
            {
                "cell_type": "code",
                "execution_count": None,
                "id": "dlai-shared-code-path",
                "metadata": {},
                "outputs": [],
                "source": _notebook_shared_path_bootstrap(
                    _notebook_code_root_expression(path, group_dir)
                ),
            },
        )
        path.write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return 1
    return 0


def _rewrite_canonical_self_references(canonical_dir, folder_name):
    rewritten = 0
    pattern = re.compile(r"open\((['\"]){}\/([^'\"]+)\1".format(re.escape(folder_name)))
    for path in sorted(canonical_dir.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        def replace(match):
            return 'open(Path(__file__).resolve().parent / "{}"'.format(match.group(2))

        new_text = pattern.sub(replace, text)
        if new_text == text:
            continue
        new_text = _ensure_path_import(new_text)
        path.write_text(new_text, encoding="utf-8")
        rewritten += 1
    return rewritten


def _imports_folder(text, folder_name):
    pattern = re.compile(
        r"^\s*(?:from\s+{0}(?:\.|\s+import\b)|import\s+{0}(?:\.|\s|,|$))".format(
            re.escape(folder_name)
        ),
        re.MULTILINE,
    )
    return bool(pattern.search(text))


def _first_folder_import_line(lines, folder_name):
    pattern = re.compile(
        r"^\s*(?:from\s+{0}(?:\.|\s+import\b)|import\s+{0}(?:\.|\s|,|$))".format(
            re.escape(folder_name)
        )
    )
    for index, line in enumerate(lines):
        if pattern.search(line):
            return index
    return None


def _python_shared_path_bootstrap(root_expression):
    return (
        "import sys\n"
        "from pathlib import Path\n\n"
        "{marker} = {root_expression}\n"
        "if str({marker}) not in sys.path:\n"
        "    sys.path.insert(0, str({marker}))\n\n"
    ).format(marker=_SHARED_CODE_MARKER, root_expression=root_expression)


def _notebook_shared_path_bootstrap(root_expression):
    return (
        "import sys\n"
        "from pathlib import Path\n\n"
        "{marker} = {root_expression}\n"
        "if str({marker}) not in sys.path:\n"
        "    sys.path.insert(0, str({marker}))"
    ).format(marker=_SHARED_CODE_MARKER, root_expression=root_expression)


def _python_code_root_expression(path, group_dir):
    depth = len(path.parent.relative_to(group_dir).parts)
    return "Path(__file__).resolve().parents[{}]".format(depth)


def _notebook_code_root_expression(path, group_dir):
    depth = len(path.parent.relative_to(group_dir).parts)
    if depth <= 0:
        return "Path.cwd().resolve()"
    return "Path.cwd().resolve().parents[{}]".format(depth - 1)


def _ensure_path_import(text):
    if re.search(r"^from\s+pathlib\s+import\s+.*\bPath\b", text, re.MULTILINE):
        return text
    lines = text.splitlines(keepends=True)
    lines.insert(_python_import_insert_index(lines), "from pathlib import Path\n")
    return "".join(lines)


def _python_import_insert_index(lines):
    index = 0
    if lines and lines[0].startswith("#!"):
        index = 1
    if len(lines) > index and re.match(r"#.*coding[:=]\s*[-\w.]+", lines[index]):
        index += 1
    while index < len(lines) and lines[index].startswith("from __future__ import "):
        index += 1
    return index


def _cell_source_text(cell):
    source = cell.get("source", "")
    if isinstance(source, list):
        return "".join(source)
    return str(source)


def _summary_prefix(group, directory, group_dir):
    relative = directory.relative_to(group_dir).as_posix()
    return "{}/{}".format(group, relative) if relative != "." else group


def _is_relative_to(path, base):
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def parse_jupyter_contents_location(url):
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]

    if "api" in parts and "contents" in parts:
        contents_index = parts.index("contents")
        if contents_index == 0 or parts[contents_index - 1] != "api":
            raise CodeDownloadError("Code URL must point to a Jupyter contents API.")
        base_parts = parts[: contents_index + 1]
        root_parts = parts[contents_index + 1 :]
        return ContentsApiLocation(
            base_url=_url_with_path(parsed, base_parts),
            root_path="/".join(root_parts),
            query=parsed.query,
        )

    if "tree" not in parts:
        raise CodeDownloadError("Code URL must be a Jupyter /tree or /api/contents URL.")

    tree_index = parts.index("tree")
    if tree_index > 0 and parts[tree_index - 1] == "lab":
        prefix_parts = parts[: tree_index - 1]
    else:
        prefix_parts = parts[:tree_index]
    root_parts = parts[tree_index + 1 :]

    return ContentsApiLocation(
        base_url=_url_with_path(parsed, [*prefix_parts, "api", "contents"]),
        root_path="/".join(root_parts),
        query=parsed.query,
    )


def extract_jupyter_lab_links(html, lesson_url="", group=CODE_GROUP_LESSONS):
    soup = BeautifulSoup(html, "lxml")
    links = []
    seen = set()
    group = _normalize_code_group(group)

    for iframe in soup.find_all("iframe", src=True):
        src = iframe["src"]
        if "lab-aws-production.deeplearning.ai" not in src:
            continue
        token = _token_from_url(src)
        if not token:
            continue
        tree_url = jupyter_tree_url_from_iframe_src(src)
        key = (redact_url(tree_url), token)
        if key in seen:
            continue
        seen.add(key)
        links.append(JupyterLabLink(tree_url, token=token, lesson_url=lesson_url, group=group))

    return links


def jupyter_tree_url_from_iframe_src(src):
    parsed = urlparse(src)
    path = parsed.path

    if "/notebooks/" in path:
        root_path = path.split("/notebooks/", 1)[0]
    elif "/lab/tree/" in path:
        root_path = path.split("/lab/tree/", 1)[0]
    elif "/tree/" in path:
        root_path = path.split("/tree/", 1)[0]
    else:
        root_path = ""

    tree_path = "{}/tree".format(root_path.rstrip("/")) if root_path else "/tree"
    token = _token_from_url(src)
    query = urlencode({"token": token}) if token else ""
    return urlunparse((parsed.scheme, parsed.netloc, tree_path, "", query, ""))


def redact_url(text):
    return re.sub(r"([?&]token=)[^&\s\"']+", r"\1REDACTED", str(text))


def _url_with_path(parsed, parts):
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            "/" + _quote_path("/".join(parts)),
            "",
            "",
            "",
        )
    )


def _token_from_url(url):
    return parse_qs(urlparse(url).query).get("token", [""])[0]


def _query_with_content(query, token=None):
    pairs = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key not in {"content", "token"}
    ]
    if token is not None:
        pairs.append(("token", token))
    else:
        pairs.extend((key, value) for key, value in parse_qsl(query, keep_blank_values=True) if key == "token")
    pairs.append(("content", "1"))
    return urlencode(pairs)


def _query_without_content(query, token=None):
    pairs = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key not in {"content", "token"}
    ]
    if token is not None:
        pairs.append(("token", token))
    else:
        pairs.extend((key, value) for key, value in parse_qsl(query, keep_blank_values=True) if key == "token")
    return urlencode(pairs)


def _quote_path(path):
    return "/".join(quote(part) for part in path.split("/") if part)


def _clean_remote_path(path):
    return str(path or "").strip("/")


def _relative_remote_path(remote_path, root_path, name):
    remote_path = _clean_remote_path(remote_path)
    root_path = _clean_remote_path(root_path)
    if root_path and remote_path == root_path:
        return name or PurePosixPath(remote_path).name
    if root_path and remote_path.startswith(root_path + "/"):
        return remote_path[len(root_path) + 1 :]
    return remote_path or name


def _should_skip_directory(path, name):
    parts = PurePosixPath(_clean_remote_path(path) or name).parts
    return any(part in SKIP_DIRECTORY_NAMES or part.startswith(".") for part in parts)


def _should_skip_file(relative_path, name):
    filename = name or PurePosixPath(relative_path).name
    return filename in SKIP_FILE_NAMES


def _host_tokens(links):
    tokens = {}
    for link in links:
        token = link.token or _token_from_url(link.url)
        if not token:
            continue
        host = urlparse(link.url).netloc
        tokens.setdefault(host, token)
    return tokens


def _source_identity(url):
    try:
        location = parse_jupyter_contents_location(url)
    except CodeDownloadError:
        parsed = urlparse(url)
        return (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/"),
        )

    parsed = urlparse(location.base_url)
    return (
        parsed.scheme,
        parsed.netloc,
        parsed.path.rstrip("/"),
        _clean_remote_path(location.root_path),
    )


def _normalize_code_group(group):
    return group if group in CODE_GROUPS else CODE_GROUP_LESSONS


def _asset_path(group, relative_path):
    relative_path = str(relative_path or "").strip("/")
    group = _normalize_code_group(group)
    return "{}/{}".format(group, relative_path) if relative_path else group


def _safe_output_path(output_dir, relative_path):
    path = PurePosixPath(str(relative_path))
    if path.is_absolute():
        return None
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None

    root = Path(output_dir).resolve()
    candidate = (root / Path(*path.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _entry_bytes(entry):
    content = entry.get("content")
    if content is None:
        raise CodeDownloadError("file content is missing")

    if entry.get("type") == "notebook":
        if isinstance(content, str):
            return content.encode("utf-8")
        return (json.dumps(content, ensure_ascii=False, indent=1) + "\n").encode("utf-8")

    if entry.get("format") == "base64":
        return base64.b64decode(content)

    if isinstance(content, str):
        return content.encode("utf-8")

    return (json.dumps(content, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _is_jupyter_auth_error(exc):
    status = getattr(exc, "status", None)
    if status in {401, 403}:
        return True
    message = str(exc)
    return (
        "HTTP 401" in message
        or "HTTP 403" in message
        or "Timeout" in message
        or "Token authentication is enabled" in message
    )


def _jupyter_auth_message(location):
    host = urlparse(location.base_url).netloc
    return (
        "Jupyter authentication or a live lab session is required for {}. Safari cookies "
        "are not reused by Playwright; set code_token in dlai-transcripts.json, include "
        "?token=... in code_url, or set browser_visibility to visible and log in in the "
        "opened browser.".format(host)
    )
