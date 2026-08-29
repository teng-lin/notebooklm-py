#!/usr/bin/env python3
"""Regenerate or verify the checked-in Android protobuf artifacts.

Generation is deliberately hermetic with respect to repository inputs: the
tool versions and flags are exact, input files are sorted, and output is first
written to a temporary tree. The default mode byte-compares that tree and its
descriptor set with the checked-in artifacts. ``--write`` updates them after a
reviewed schema or toolchain change.
"""

from __future__ import annotations

import argparse
import difflib
import importlib.metadata
import subprocess
import sys
import tempfile
from pathlib import Path

EXPECTED_DISTRIBUTIONS = {
    "grpcio": "1.76.0",
    "grpcio-tools": "1.76.0",
    "protobuf": "6.31.1",
}
EXPECTED_PROTOC = "libprotoc 31.1"

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "notebooklm" / "_android" / "proto_src"
OUTPUT_ROOT = REPO_ROOT / "src" / "notebooklm" / "_android" / "proto"
DESCRIPTOR_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "android" / "android_descriptor_set.pb"
PROTO_FILES = (
    Path("google/internal/labs/tailwind/orchestration/v1/b1_read.proto"),
    Path("google/internal/labs/tailwind/orchestration/v1/b3_sources.proto"),
    Path("google/internal/labs/tailwind/v1/source_settings.proto"),
    Path("notebooklm/internal/android/wire/v1/b2_notebooks.proto"),
    Path("notebooklm/internal/android/wire/source_mutation_wire.proto"),
)
EXPECTED_GENERATED = frozenset(
    {
        Path("google/internal/labs/tailwind/orchestration/v1/b1_read_pb2.py"),
        Path("google/internal/labs/tailwind/orchestration/v1/b1_read_pb2_grpc.py"),
        Path("google/internal/labs/tailwind/orchestration/v1/b3_sources_pb2.py"),
        Path("google/internal/labs/tailwind/orchestration/v1/b3_sources_pb2_grpc.py"),
        Path("google/internal/labs/tailwind/v1/source_settings_pb2.py"),
        Path("google/internal/labs/tailwind/v1/source_settings_pb2_grpc.py"),
        Path("notebooklm/internal/android/wire/v1/b2_notebooks_pb2.py"),
        Path("notebooklm/internal/android/wire/v1/b2_notebooks_pb2_grpc.py"),
        Path("notebooklm/internal/android/wire/source_mutation_wire_pb2.py"),
        Path("notebooklm/internal/android/wire/source_mutation_wire_pb2_grpc.py"),
    }
)

_GENERATED_IMPORT_PREFIX = "from google.internal.labs.tailwind"
_REPOSITORY_IMPORT_PREFIX = "from notebooklm._android.proto.google.internal.labs.tailwind"
_GENERATED_LOCAL_PREFIX = "from notebooklm.internal.android.wire"
_REPOSITORY_LOCAL_PREFIX = "from notebooklm._android.proto.notebooklm.internal.android.wire"


def _verify_toolchain() -> None:
    problems: list[str] = []
    for distribution, expected in EXPECTED_DISTRIBUTIONS.items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"{distribution} is not installed (expected {expected})")
        else:
            if actual != expected:
                problems.append(f"{distribution} is {actual}, expected {expected}")

    try:
        protoc = subprocess.run(
            [sys.executable, "-m", "grpc_tools.protoc", "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        problems.append(f"cannot execute pinned grpc_tools.protoc: {exc}")
    else:
        if protoc != EXPECTED_PROTOC:
            problems.append(f"protoc is {protoc!r}, expected {EXPECTED_PROTOC!r}")

    if problems:
        raise RuntimeError("Android protobuf toolchain mismatch:\n- " + "\n- ".join(problems))


def _compile(temp_root: Path) -> tuple[Path, Path]:
    import grpc_tools
    from grpc_tools import protoc

    generated_root = temp_root / "generated"
    descriptor_path = temp_root / "android_descriptor_set.pb"
    generated_root.mkdir(parents=True)
    bundled_well_known_types = Path(grpc_tools.__file__).resolve().parent / "_proto"

    args = [
        "grpc_tools.protoc",
        f"-I{SOURCE_ROOT}",
        f"-I{bundled_well_known_types}",
        "--include_imports",
        f"--descriptor_set_out={descriptor_path}",
        f"--python_out={generated_root}",
        f"--grpc_python_out={generated_root}",
        *(str(path) for path in sorted(PROTO_FILES)),
    ]
    result = protoc.main(args)
    if result != 0:
        raise RuntimeError(f"grpc_tools.protoc failed with exit status {result}")

    _relocate_generated_imports(generated_root)
    actual_generated = _generated_files(generated_root)
    if actual_generated != EXPECTED_GENERATED:
        raise RuntimeError(
            "generated file set differs from the pinned Android closure: "
            f"expected={sorted(map(str, EXPECTED_GENERATED))}, "
            f"actual={sorted(map(str, actual_generated))}"
        )
    return generated_root, descriptor_path


def _generated_files(root: Path) -> frozenset[Path]:
    return frozenset(
        path.relative_to(root)
        for path in root.rglob("*.py")
        if path.name.endswith(("_pb2.py", "_pb2_grpc.py"))
    )


def _relocate_generated_imports(generated_root: Path) -> None:
    """Make protoc's exact-source imports resolve inside ``notebooklm``.

    Protobuf has no Python-package option. The source descriptor/import paths
    remain exact (``google/internal/...``), while this deterministic generated
    code rewrite places imports under the private repository package.
    """
    for path in sorted(generated_root.rglob("*.py")):
        content = path.read_text(encoding="utf-8")
        relocated = content.replace(_GENERATED_IMPORT_PREFIX, _REPOSITORY_IMPORT_PREFIX)
        relocated = relocated.replace(_GENERATED_LOCAL_PREFIX, _REPOSITORY_LOCAL_PREFIX)
        path.write_text(relocated, encoding="utf-8")


def _compare_text(expected: Path, actual: Path, relative: Path) -> list[str]:
    if not expected.is_file():
        return [f"missing generated artifact: {relative}"]
    expected_text = expected.read_text(encoding="utf-8")
    actual_text = actual.read_text(encoding="utf-8")
    if expected_text == actual_text:
        return []
    diff = "".join(
        difflib.unified_diff(
            expected_text.splitlines(keepends=True),
            actual_text.splitlines(keepends=True),
            fromfile=f"checked-in/{relative}",
            tofile=f"regenerated/{relative}",
        )
    )
    return [diff]


def _check(generated_root: Path, descriptor_path: Path) -> None:
    problems: list[str] = []
    checked_in = _generated_files(OUTPUT_ROOT)
    if checked_in != EXPECTED_GENERATED:
        problems.append(
            "checked-in generated file set differs from the Android closure: "
            f"expected={sorted(map(str, EXPECTED_GENERATED))}, "
            f"actual={sorted(map(str, checked_in))}"
        )
    for relative in sorted(EXPECTED_GENERATED):
        problems.extend(_compare_text(OUTPUT_ROOT / relative, generated_root / relative, relative))

    if not DESCRIPTOR_FIXTURE.is_file():
        problems.append(f"missing descriptor fixture: {DESCRIPTOR_FIXTURE.relative_to(REPO_ROOT)}")
    elif DESCRIPTOR_FIXTURE.read_bytes() != descriptor_path.read_bytes():
        problems.append("checked-in Android descriptor set differs from pinned regeneration")

    if problems:
        raise RuntimeError("Android protobuf regeneration check failed:\n" + "\n".join(problems))


def _write(generated_root: Path, descriptor_path: Path) -> None:
    for relative in sorted(EXPECTED_GENERATED):
        destination = OUTPUT_ROOT / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((generated_root / relative).read_bytes())
    DESCRIPTOR_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    DESCRIPTOR_FIXTURE.write_bytes(descriptor_path.read_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify checked-in artifacts (the default)",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="update checked-in generated modules and descriptor fixture",
    )
    args = parser.parse_args(argv)

    try:
        _verify_toolchain()
        with tempfile.TemporaryDirectory(prefix="notebooklm-android-proto-") as temp_dir:
            generated_root, descriptor_path = _compile(Path(temp_dir))
            if args.write:
                _write(generated_root, descriptor_path)
                print("Updated checked-in Android protobuf artifacts")
            else:
                _check(generated_root, descriptor_path)
                print("OK: Android protobuf descriptors and generated tree are deterministic")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
