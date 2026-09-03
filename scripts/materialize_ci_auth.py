#!/usr/bin/env python3
"""Materialize one selected CI token and mint a fresh auth profile."""

from __future__ import annotations

import argparse
import os
import random
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from notebooklm._logging import scrub_secrets

CONFIGURATION_ERROR = 2
AUTH_ERROR = 3
INFRASTRUCTURE_ERROR = 4
_SLOTS = ("A", "B", "C")
_LANES = (
    "nightly-web-ubuntu",
    "nightly-android-macos",
    "nightly-readonly-windows",
    "rpc-health-web",
    "rpc-health-android",
    "verify-package",
)
_PROFILE_RE = re.compile(
    rf"ci-(?P<slot>[A-C])-(?P<lane>{'|'.join(re.escape(lane) for lane in _LANES)})\Z"
)
MINT_TIMEOUT_SECONDS = 600


class ConfigurationError(ValueError):
    pass


class AuthenticationError(RuntimeError):
    pass


class InfrastructureError(RuntimeError):
    pass


def validate_binding(account_slot: str, profile: str) -> None:
    if account_slot not in _SLOTS:
        raise ConfigurationError("account slot must be A, B, or C")
    match = _PROFILE_RE.fullmatch(profile)
    if match is None or match.group("slot") != account_slot:
        raise ConfigurationError("profile must be ci-<selected-slot>-<allowlisted-lane>")


def profile_dir(profile: str, env: Mapping[str, str]) -> Path:
    root = Path(env.get("NOTEBOOKLM_HOME", str(Path.home() / ".notebooklm"))).expanduser()
    return root / "profiles" / profile


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise InfrastructureError("profile path is not a regular directory")
    if os.name == "nt" and path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise InfrastructureError("profile path is a reparse point")
    if os.name != "nt":
        path.chmod(0o700)
        if stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise InfrastructureError("profile directory permissions are not private")


def atomic_private_write(path: Path, value: str) -> None:
    """Atomically write an opaque credential with private POSIX permissions."""
    _ensure_private_directory(path.parent)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        if path.is_symlink() or not path.is_file():
            raise InfrastructureError("credential path is not a regular file")
        if os.name == "nt" and path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise InfrastructureError("credential path is a reparse point")
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise InfrastructureError("credential file permissions are not private")
        if os.name != "nt":
            # Persist the directory entry as well as the temporary file contents.
            # Without this fsync, a power loss can lose an otherwise successful
            # replace even though the file itself was flushed.
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def materialize(
    *,
    account_slot: str,
    profile: str,
    env: Mapping[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    randint: Callable[[int, int], int] = random.SystemRandom().randint,
) -> int:
    source_env = dict(os.environ if env is None else env)
    validate_binding(account_slot, profile)
    token = source_env.get("NOTEBOOKLM_MASTER_TOKEN_JSON", "")
    if not token:
        raise ConfigurationError(
            f"NOTEBOOKLM_MASTER_TOKEN_JSON resolved empty for slot {account_slot}"
        )

    target_dir = profile_dir(profile, source_env)
    token_path = target_dir / "master_token.json"
    storage_path = target_dir / "storage_state.json"
    atomic_private_write(token_path, token)
    try:
        storage_path.unlink()
    except FileNotFoundError:
        pass

    child_env = source_env.copy()
    child_env.pop("NOTEBOOKLM_MASTER_TOKEN_JSON", None)
    for name in tuple(child_env):
        if name == "NOTEBOOKLM_AUTH_JSON" or name.startswith("NOTEBOOKLM_MASTER_TOKEN_JSON_"):
            child_env.pop(name, None)
    child_env["NOTEBOOKLM_PROFILE"] = profile
    command = [sys.executable, "-m", "notebooklm", "login", "--master-token-refresh"]

    last_result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, 4):
        try:
            last_result = run(
                command,
                env=child_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=MINT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            last_result = subprocess.CompletedProcess(
                command,
                124,
                "",
                f"auth mint timed out after {MINT_TIMEOUT_SECONDS} seconds",
            )
        except Exception as exc:
            raise InfrastructureError("could not launch the auth mint subprocess") from exc

        for output in (last_result.stdout, last_result.stderr):
            # The shared scrubber catches credential-shaped fields, while the
            # selected master-token file is intentionally opaque and may have
            # no recognizable shape. Redact its exact bytes as well.
            sanitized = scrub_secrets((output or "").replace(token, "***")).strip()
            if sanitized:
                print(sanitized, file=sys.stderr)
        if last_result.returncode == 0:
            if not storage_path.is_file() or storage_path.is_symlink():
                raise InfrastructureError("auth mint returned success without fresh storage")
            if (
                os.name == "nt"
                and storage_path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise InfrastructureError("auth storage path is a reparse point")
            if os.name != "nt":
                storage_path.chmod(0o600)
                if stat.S_IMODE(storage_path.stat().st_mode) != 0o600:
                    raise InfrastructureError("auth storage permissions are not private")
            print(
                f"Auth profile {profile} materialized for slot {account_slot} "
                f"after {attempt} attempt(s)"
            )
            return attempt
        if attempt < 3:
            sleep(attempt * 15 + randint(0, 19))

    code = last_result.returncode if last_result is not None else "unknown"
    raise AuthenticationError(f"master-token mint failed after 3 attempts (exit {code})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-slot", required=True, choices=_SLOTS)
    parser.add_argument("--profile", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        materialize(account_slot=args.account_slot, profile=args.profile)
    except ConfigurationError as exc:
        print(f"configuration error: {scrub_secrets(exc)}", file=sys.stderr)
        return CONFIGURATION_ERROR
    except AuthenticationError as exc:
        print(f"authentication error: {scrub_secrets(exc)}", file=sys.stderr)
        return AUTH_ERROR
    except (InfrastructureError, OSError) as exc:
        print(f"infrastructure error: {scrub_secrets(exc)}", file=sys.stderr)
        return INFRASTRUCTURE_ERROR
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
