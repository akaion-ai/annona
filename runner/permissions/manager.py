"""
Permission Manager

Policy checks for runner operations.
"""

import re
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger


class PermissionManager:
    """Evaluates the runner's permission policy."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.permissions = config.get("permissions", {})

        # Filesystem permissions
        self.fs_perms = self.permissions.get("filesystem", {})
        self.allowed_paths = self._expand_paths(self.fs_perms.get("allowed_paths", []))
        self.denied_paths = self._expand_paths(self.fs_perms.get("denied_paths", []))
        self.max_file_size = self.fs_perms.get("max_file_size_mb", 100) * 1024 * 1024

        # Shell permissions
        self.shell_perms = self.permissions.get("shell", {})
        self.shell_enabled = self.shell_perms.get("enabled", True)
        self.allowed_commands = self.shell_perms.get("allowed_commands", [])
        self.denied_commands = self.shell_perms.get("denied_commands", [])

        # Network permissions
        self.network_perms = self.permissions.get("network", {})
        self.network_enabled = self.network_perms.get("enabled", True)
        self.allowed_domains = self.network_perms.get("allowed_domains", [])

        # System permissions
        self.system_perms = self.permissions.get("system", {})
        self.allow_env_vars = self.system_perms.get("allow_env_vars", False)
        self.allow_process_mgmt = self.system_perms.get("allow_process_management", False)

    @staticmethod
    def _expand_paths(paths: List[str]) -> List[Path]:
        """Espande i path con ~ e variabili d'ambiente"""
        expanded = []
        for path_str in paths:
            path = Path(path_str).expanduser()
            expanded.append(path.resolve())
        return expanded

    def check_tool_permission(self, tool_name: str, args: Dict[str, Any]) -> bool:
        """
        Whether a tool may run with the given arguments

        Args:
            tool_name: Nome del tool
            args: Argomenti per il tool

        Returns:
            True if allowed, False otherwise
        """
        # Routing per tool type
        if tool_name in ["read_file", "write_file", "list_dir", "filesystem"]:
            return self._check_filesystem_permission(args)

        elif tool_name in ["shell", "execute", "run_command"]:
            return self._check_shell_permission(args)

        elif tool_name in ["http_request", "fetch_url", "browser"]:
            return self._check_network_permission(args)

        elif tool_name in ["env", "process"]:
            return self._check_system_permission(tool_name)

        # Default: permetti
        return True

    def _check_filesystem_permission(self, args: Dict[str, Any]) -> bool:
        """Check filesystem policy."""
        path_arg = args.get("path") or args.get("file") or args.get("directory")

        if not path_arg:
            return True

        target_path = Path(path_arg).expanduser().resolve()

        # Controlla denied paths
        for denied in self.denied_paths:
            if self._is_path_under(target_path, denied):
                logger.warning(f"Permission denied: path {target_path} is in denied list")
                return False

        # Controlla allowed paths
        if self.allowed_paths:
            for allowed in self.allowed_paths:
                if self._is_path_under(target_path, allowed):
                    return True

            logger.warning(f"Permission denied: path {target_path} not in allowed list")
            return False

        # Se non ci sono allowed_paths definiti, permetti tutto (eccetto denied)
        return True

    def _check_shell_permission(self, args: Dict[str, Any]) -> bool:
        """Check shell policy."""
        if not self.shell_enabled:
            logger.warning("Shell execution is disabled")
            return False

        command = args.get("command") or args.get("cmd")

        if not command:
            return True

        # Take the base command, the first token
        cmd_base = command.strip().split()[0] if command else ""

        # Controlla denied commands
        for denied in self.denied_commands:
            if self._match_pattern(command, denied) or cmd_base == denied:
                logger.warning(f"Permission denied: command '{cmd_base}' is denied")
                return False

        # Controlla allowed commands
        if self.allowed_commands:
            for allowed in self.allowed_commands:
                if self._match_pattern(command, allowed) or cmd_base == allowed:
                    return True

            logger.warning(f"Permission denied: command '{cmd_base}' not in allowed list")
            return False

        # Se non ci sono allowed_commands, permetti tutto (eccetto denied)
        return True

    def _check_network_permission(self, args: Dict[str, Any]) -> bool:
        """Check network policy."""
        if not self.network_enabled:
            logger.warning("Network access is disabled")
            return False

        url = args.get("url") or args.get("domain")

        if not url:
            return True

        # Estrai domain dall'URL
        from urllib.parse import urlparse

        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path

        # Se non ci sono domini consentiti, permetti tutto
        if not self.allowed_domains:
            return True

        # Controlla allowed domains
        for allowed in self.allowed_domains:
            if self._match_domain(domain, allowed):
                return True

        logger.warning(f"Permission denied: domain '{domain}' not in allowed list")
        return False

    def _check_system_permission(self, tool_name: str) -> bool:
        """Check system policy."""
        if tool_name == "env":
            if not self.allow_env_vars:
                logger.warning("Environment variable access is disabled")
                return False

        if tool_name == "process":
            if not self.allow_process_mgmt:
                logger.warning("Process management is disabled")
                return False

        return True

    @staticmethod
    def _is_path_under(path: Path, parent: Path) -> bool:
        """Whether path lives under parent."""
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def _match_pattern(text: str, pattern: str) -> bool:
        """Match con wildcard support"""
        # Converti wildcard in regex
        regex_pattern = pattern.replace("*", ".*").replace("?", ".")
        return bool(re.match(f"^{regex_pattern}$", text))

    @staticmethod
    def _match_domain(domain: str, pattern: str) -> bool:
        """Match domain con wildcard support"""
        # *.example.com -> qualsiasi subdomain
        if pattern.startswith("*."):
            base = pattern[2:]
            return domain == base or domain.endswith("." + base)

        return domain == pattern

    def validate_file_size(self, file_path: Path) -> bool:
        """Valida che un file non superi la dimensione massima"""
        if not file_path.exists():
            return True

        size = file_path.stat().st_size

        if size > self.max_file_size:
            logger.warning(f"File {file_path} exceeds max size ({size} > {self.max_file_size})")
            return False

        return True
