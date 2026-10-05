"""Node-editor extension registry: static-file sync and DB-backed serving.

Extensions used to be plain files discovered on disk. The registry model adds
enable/disable plus the ability to upload extensions through the admin without
filesystem access. Static files still auto-register (enabled) on manifest read,
so dropping a file in ``extensions_editor/`` keeps working.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings

from ..constants import BLOCKED_JS_PATTERNS, WARN_JS_PATTERNS
from ..models import EditorExtension
from ..repositories.editor_extension_repository import (
    EditorExtensionRepository,
)

EXTENSION_NAME_RE = re.compile(r'^[a-z0-9_]+$')

# Optional self-declared identity in the source header:
#   /** ... @s7-editor my_extension ... */
# When present it wins over the uploaded filename.
NAME_TAG_RE = re.compile(r'@s7-editor\s+([a-zA-Z0-9_]+)')

_BLOCKED_RES = tuple((re.compile(p), reason) for p, reason in
                     BLOCKED_JS_PATTERNS)
_WARN_RES = tuple((re.compile(p), reason) for p, reason in
                  WARN_JS_PATTERNS)

SEMGREP_RULES = (
    Path(__file__).resolve().parent.parent / 'security' / 'js_rules.yml'
)

SCAN_MODES = ('strict', 'warn', 'off')

logger = logging.getLogger(__name__)


class EditorExtensionService:
    """Business logic for the editor-extension registry."""

    def list_manifest(self, extensions_url_base: str) -> list[dict]:
        """Enabled extensions as [{name, url}] for the editor frontend."""
        self.sync_static()
        return [
            {
                'name': ext.name,
                'url': f'{extensions_url_base}{ext.filename}',
            }
            for ext in self.repository.list_enabled()
        ]

    def get_source(self, name: str) -> str | None:
        """JS source for an enabled extension, or None if not servable."""
        ext = self.repository.get_by_name(name)
        if ext is None or not ext.is_enabled:
            return None
        if ext.source == EditorExtension.SOURCE_DB:
            return ext.content
        return self._read_static_file(ext.filename)

    MAX_UPLOAD_BYTES = 256 * 1024
    # Every extension must register through the window.s7Editors API.
    REGISTRATION_TOKEN = 's7Editors'

    @staticmethod
    def resolve_name(filename: str, content: str | None = None) -> str:
        """Canonical extension name: ``@s7-editor`` tag > filename stem."""
        if content:
            tag = NAME_TAG_RE.search(content)
            if tag:
                return tag.group(1).lower()
        if filename.endswith('.js'):
            return filename[:-3]
        return filename

    def __init__(self) -> None:
        self.repository = EditorExtensionRepository()
        # strict: semgrep findings block; warn: findings become warnings;
        # off: skip the external scanner entirely (denylist still applies).
        self.scan_mode = os.environ.get(
            'S7_EXTENSION_SCAN', 'strict',
        ).lower()
        if self.scan_mode not in SCAN_MODES:
            self.scan_mode = 'strict'
        # Warnings produced by the most recent upload()/validate_source().
        self.last_warnings: list[str] = []

    def upload(
        self, filename: str, content: str, description: str = '',
    ) -> EditorExtension:
        """Store uploaded JS as a DB-sourced extension row.

        Validations: .js filename, size cap, non-empty UTF-8 source,
        presence of the s7Editors registration contract, a blocked-pattern
        denylist, and an optional semgrep scan (when the binary exists).
        Non-blocking findings land on ``self.last_warnings``.
        """
        name = self.resolve_name(filename, content)
        if not EXTENSION_NAME_RE.match(name):
            raise ValueError(
                f'Invalid extension name {name!r}: use lowercase '
                'letters, digits and underscores',
            )
        filename = f'{name}.js'

        errors, warnings = self.validate_source(content)
        if errors:
            raise ValueError('Invalid extension source: ' + '; '.join(errors))

        return self.repository.upsert_uploaded(
            name=name, filename=filename,
            content=content, description=description,
        )

    def validate_source(self, content: str) -> tuple[list[str], list[str]]:
        """Sanity + security checks on extension source code.

        Returns ``(errors, warnings)``: errors reject the upload, warnings
        are surfaced to the admin but allowed.
        """
        errors: list[str] = []
        warnings: list[str] = []
        self.last_warnings = warnings
        if not isinstance(content, str) or not content.strip():
            errors.append('empty source')
            return errors, warnings
        if len(content.encode('utf-8')) > self.MAX_UPLOAD_BYTES:
            errors.append(
                f'exceeds {self.MAX_UPLOAD_BYTES // 1024}KB',
            )
        if self.REGISTRATION_TOKEN not in content:
            errors.append(
                'does not reference window.s7Editors — extensions must '
                'register a renderer/check/save handler',
            )
        for pattern, reason in _BLOCKED_RES:
            if pattern.search(content):
                errors.append(f'blocked pattern: {reason}')
        for pattern, reason in _WARN_RES:
            if pattern.search(content):
                warnings.append(f'flagged: {reason}')

        findings = self._semgrep_findings(content)
        if findings:
            for severity, desc in findings:
                if severity == 'WARNING' or self.scan_mode == 'warn':
                    warnings.append(f'semgrep: {desc}')
                else:
                    errors.append(f'semgrep: {desc}')
        return errors, warnings

    # ------------------------------------------------------------------ #
    # Optional semgrep scan                                               #
    # ------------------------------------------------------------------ #

    def _semgrep_findings(
        self, content: str,
    ) -> list[tuple[str, str]] | None:
        """Run semgrep over the source when the binary is available.

        Returns ``(severity, description)`` findings, [] when clean, or
        None when semgrep is unavailable/fails (fail-open — the denylist
        above is the always-on baseline).
        """
        if self.scan_mode == 'off' or not SEMGREP_RULES.exists():
            return None
        binary = shutil.which('semgrep')
        if binary is None:
            return None

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                'w', suffix='.js', delete=False, encoding='utf-8',
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            proc = subprocess.run(
                [
                    binary, 'scan', '--json', '--quiet', '--metrics=off',
                    '--config', str(SEMGREP_RULES), tmp_path,
                ],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning('semgrep scan skipped: %s', e)
            return None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        try:
            results = json.loads(proc.stdout or '{}').get('results', [])
        except json.JSONDecodeError:
            logger.warning('semgrep returned non-JSON output')
            return None
        return [
            (
                r.get('extra', {}).get('severity', 'ERROR'),
                f"{r.get('check_id')} (line "
                f"{r.get('start', {}).get('line')}): "
                f"{r.get('extra', {}).get('message', '')}",
            )
            for r in results
            if r.get('extra', {}).get('severity') in ('ERROR', 'WARNING')
        ]

    # ------------------------------------------------------------------ #
    # Static-file discovery                                               #
    # ------------------------------------------------------------------ #

    def sync_static(self) -> None:
        """Reconcile the registry with .js files present on disk.

        Registers a row (enabled) for every file found and disables
        static-sourced rows whose file was removed — rows are kept, not
        deleted, so descriptions/toggles survive a file's return.
        """
        directory = self._static_extensions_dir()
        if directory is None:
            return
        present = set()
        for path in sorted(directory.glob('*.js')):
            if path.name.startswith('.'):
                continue
            present.add(path.stem)
            self.repository.register_static(path.stem, path.name)
        self.repository.disable_missing_static(present)

    def _read_static_file(self, filename: str) -> str | None:
        directory = self._static_extensions_dir()
        if directory is None:
            return None
        path = directory / filename
        if not path.exists():
            return None
        return path.read_text(encoding='utf-8')

    @staticmethod
    def _static_extensions_dir() -> Path | None:
        # Development: STATICFILES_DIRS (source files);
        # production: STATIC_ROOT (collected files).
        for static_dir in getattr(settings, 'STATICFILES_DIRS', []):
            candidate = Path(static_dir) / 'admin' / 'js' / 'extensions_editor'
            if candidate.exists():
                return candidate
        static_root = getattr(settings, 'STATIC_ROOT', None)
        if static_root:
            candidate = (
                Path(static_root) / 'admin' / 'js' / 'extensions_editor'
            )
            if candidate.exists():
                return candidate
        return None
