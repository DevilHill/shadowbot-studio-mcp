"""Opt-in, atomic CodeFlow source writes; Studio synchronization is the caller's job.

Only configured projects and existing CodeFlows are accepted. This module never
rewrites flow metadata, saves/reloads Studio, executes code, or rolls back a write.
"""
import ast
import codecs
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


ROOT = Path(__file__).resolve().parent
MAX_SOURCE_BYTES = 8 * 1024 * 1024


class BackendError(RuntimeError):
    pass


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _normalized(source):
    return source.replace("\r\n", "\n").replace("\r", "\n")


def _source_digest(source):
    return _digest(_normalized(source).encode("utf-8"))


def _safe_path(path, *, directory=False):
    """Check the un-resolved path so junction/symlink ancestors cannot disappear."""
    path = Path(path)
    if not path.is_absolute():
        raise BackendError("The configured project path must be absolute.")
    for candidate in (*reversed(path.parents), path):
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise BackendError(f"Required backend path is unavailable: {candidate}") from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BackendError(f"Symlinks and Windows reparse points are not supported: {candidate}")
    info = path.stat()
    if directory and not stat.S_ISDIR(info.st_mode):
        raise BackendError("The configured project root must be a directory.")
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise BackendError("Backend files must be regular files with one hard link.")
    return path


def _read(path):
    _safe_path(path)
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise BackendError(f"Cannot read backend file: {path}") from exc
    if len(content) > MAX_SOURCE_BYTES:
        raise BackendError("Backend file exceeds the 8 MiB limit.")
    return content


def _json(data, description):
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise BackendError(f"Invalid UTF-8 JSON in {description}.") from exc


def _flow_id(value):
    if (not isinstance(value, str) or not value or value in (".", "..")
            or value[-1] in " ." or any(c in value for c in '/\\:\x00')
            or any(ord(c) < 32 for c in value)):
        raise BackendError("flow_id must be one existing flow basename, without path separators.")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if value.split(".", 1)[0].upper() in reserved:
        raise BackendError("Windows device names are not valid flow IDs.")
    return value


def _validate_source(source):
    if not isinstance(source, str):
        raise BackendError("CodeFlow content must be a string.")
    try:
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise BackendError("CodeFlow content exceeds the 8 MiB limit.")
        tree = ast.parse(source)
    except (SyntaxError, ValueError, UnicodeError) as exc:
        raise BackendError("CodeFlow content is not valid Python source.") from exc
    uses_glv = any(isinstance(node, ast.Name) and node.id == "glv"
                   and isinstance(node.ctx, ast.Load) for node in ast.walk(tree))
    imports_glv = any(isinstance(node, ast.ImportFrom) and node.level == 1
                      and node.module == "package"
                      and any(alias.name == "variables" and alias.asname == "glv"
                              for alias in node.names) for node in tree.body)
    if uses_glv and not imports_glv:
        raise BackendError("glv usage requires a top-level 'from .package import variables as glv'.")


class _AppLock:
    def __init__(self, directory, app_id):
        self.fd = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackendError("The application lock directory is unavailable.") from exc
        _safe_path(directory, directory=True)
        path = directory / (_digest(app_id.encode("utf-8")) + ".lock")
        if path.exists() or path.is_symlink():
            _safe_path(path)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.fd = os.open(path, flags, 0o600)
            _safe_path(path)
            if os.fstat(self.fd).st_size == 0:
                os.write(self.fd, b"0")
            os.lseek(self.fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BackendError) as exc:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise BackendError("Another source operation is active, or the application lock is unavailable.") from exc

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class FileSourceBackend:
    def __init__(self, config_path=None):
        self.config_path = Path(config_path or ROOT / ".state" / "code_backends.json").absolute()

    def project_for(self, app_id):
        if not isinstance(app_id, str) or not app_id:
            raise BackendError("An exact application ID is required.")
        if not self.config_path.exists() and not self.config_path.is_symlink():
            return None
        config = _json(_read(self.config_path), "source backend configuration")
        if (not isinstance(config, dict) or type(config.get("version")) is not int
                or config["version"] != 1 or not isinstance(config.get("projects"), dict)):
            raise BackendError("Source backend configuration must contain version=1 and projects.")
        root = config["projects"].get(app_id)
        if root is None:
            return None
        if not isinstance(root, str) or not root:
            raise BackendError("Configured project roots must be absolute directory paths.")
        return _safe_path(Path(root), directory=True)

    def prepare(self, app_id, flow_id, before, desired):
        _flow_id(flow_id)
        _validate_source(desired)
        if not isinstance(before, str):
            raise BackendError("The complete source read from Studio is required.")
        project = self.project_for(app_id)
        if project is None:
            raise BackendError("The application is not configured for the file source backend.")
        lock = _AppLock(self.config_path.parent / "locks", app_id)
        try:
            return PreparedWrite(self, lock, app_id, project, flow_id, before, desired)
        except BaseException:
            lock.close()
            raise


class PreparedWrite:
    def __init__(self, backend, lock, app_id, project, flow_id, before, desired):
        self._backend, self._lock, self._app_id, self._project = backend, lock, app_id, project
        self.source_path = project / (flow_id + ".py")
        self.metadata_path = project / ".dev" / (flow_id + ".flow.json")
        self.manifest_path = project / "package.json"
        self._metadata = _read(self.metadata_path)
        metadata = _json(self._metadata, "CodeFlow metadata")
        if not isinstance(metadata, dict) or str(metadata.get("kind", "")).lower() != "code":
            raise BackendError("The target must be an existing CodeFlow, not a generated VisualFlow.")
        self._manifest = _read(self.manifest_path)
        manifest = _json(self._manifest, "project manifest")
        if (not isinstance(manifest, dict) or manifest.get("uuid") != app_id
                or not isinstance(manifest.get("flows"), list)):
            raise BackendError("The configured project manifest does not match the requested application.")
        matching = [entry for entry in manifest["flows"]
                    if isinstance(entry, dict) and entry.get("filename") == flow_id]
        if len(matching) != 1 or str(matching[0].get("kind", "")).lower() != "code":
            raise BackendError("The project manifest does not identify exactly one matching CodeFlow.")
        self._original = _read(self.source_path)
        try:
            decoded = self._original.decode("utf-8-sig")
        except UnicodeError as exc:
            raise BackendError("Only existing UTF-8 CodeFlow files are supported.") from exc
        if _source_digest(decoded) != _source_digest(before):
            raise BackendError("Disk source differs from the latest Studio source; nothing was written.")
        crlf = decoded.count("\r\n")
        bare_lf = decoded.count("\n") - crlf
        bare_cr = decoded.count("\r") - crlf
        newline = "\r\n" if crlf and crlf >= max(bare_lf, bare_cr) else "\r" if bare_cr > bare_lf else "\n"
        content = _normalized(desired).replace("\n", newline).encode("utf-8")
        self._desired = (codecs.BOM_UTF8 if self._original.startswith(codecs.BOM_UTF8) else b"") + content
        self.before_sha256 = _digest(self._original)
        self.desired_sha256 = _digest(self._desired)
        self.expected_source_sha256 = _source_digest(desired)
        self.metadata_sha256 = _digest(self._metadata)
        self._temp = None
        self._temp_identity = None
        self._closed = False
        self._commit_attempted = False
        self._committed = False
        self._verified = False
        try:
            fd, temporary = tempfile.mkstemp(prefix=f".{flow_id}.codex-", suffix=".tmp", dir=project)
            self._temp = Path(temporary)
            with os.fdopen(fd, "wb") as stream:
                info = os.fstat(stream.fileno())
                self._temp_identity = (info.st_dev, info.st_ino)
                stream.write(self._desired)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(self._temp, stat.S_IMODE(self.source_path.stat().st_mode))
        except BaseException:
            self.close()
            raise

    @property
    def committed(self):
        """True immediately after os.replace returns, including readback failure."""
        return self._committed

    @property
    def receipt(self):
        return {"backend": "file_reload", "app_id": self._app_id,
                "source_path": str(self.source_path), "before_sha256": self.before_sha256,
                "desired_sha256": self.desired_sha256,
                "expected_source_sha256": self.expected_source_sha256,
                "metadata_sha256": self.metadata_sha256,
                "manifest_sha256": _digest(self._manifest),
                "commit_attempted": self._commit_attempted,
                "file_written": self._committed, "disk_verified": self._verified}

    def _check_metadata(self):
        if self._backend.project_for(self._app_id) != self._project:
            raise BackendError("The configured project changed during this source operation.")
        if _read(self.metadata_path) != self._metadata or _read(self.manifest_path) != self._manifest:
            raise BackendError("Project metadata changed during this source operation.")

    def commit(self):
        if self._closed or self._commit_attempted:
            raise BackendError("A prepared source write can be committed only once while open.")
        self._check_metadata()
        if _read(self.source_path) != self._original:
            raise BackendError("Disk source changed after preparation; nothing was written.")
        if self._temp is None or _read(self._temp) != self._desired:
            raise BackendError("The prepared source file changed; nothing was written.")
        info = self._temp.stat()
        if (info.st_dev, info.st_ino) != self._temp_identity:
            raise BackendError("The prepared source file was replaced; nothing was written.")
        self._commit_attempted = True
        try:
            os.replace(self._temp, self.source_path)
        except OSError as exc:
            raise BackendError("Atomic source replacement failed. Inspect disk state before retrying; no rollback was attempted.") from exc
        self._committed = True
        self._temp = None
        self.verify()
        return self.receipt

    def verify(self):
        if self._closed or not self._committed:
            raise BackendError("The source write must be committed and open before verification.")
        self._verified = False
        self._check_metadata()
        if _read(self.source_path) != self._desired:
            raise BackendError("Source readback does not match the committed content. No rollback was attempted.")
        self._verified = True
        return True

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            if self._temp is not None:
                try:
                    info = self._temp.lstat()
                    if (stat.S_ISREG(info.st_mode)
                            and not getattr(info, "st_file_attributes", 0) & 0x400
                            and (info.st_dev, info.st_ino) == self._temp_identity):
                        self._temp.unlink()
                except FileNotFoundError:
                    pass
        finally:
            self._lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()
