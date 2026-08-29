#!/usr/bin/env python3
"""Extract exact unary gRPC signatures from a blutter dump.

``pp.txt`` preserves generated-client constants as adjacent entries: a
``TypeArguments: <Request, Response>`` entry followed eight bytes later by the full
``/service/method`` path.  ``ida_script/addNames.py`` supplies the protobuf-bearing Dart library
for each generated message's ``BuilderInfo._i`` symbol.  This script joins those two independent
pieces of binary evidence and writes deterministic CSV to stdout.

No package is inferred from a short type name alone.  A type must resolve uniquely either in the
service's protobuf namespace or across all recovered generated-message symbols; otherwise the
extraction fails.
"""

import argparse
import csv
import dataclasses
import re
import sys
from collections import defaultdict

TYPE_ARGUMENTS_RE = re.compile(
    r"^\[pp\+0x(?P<offset>[0-9A-Fa-f]+)\] TypeArguments: "
    r"<(?P<request>[A-Za-z_$][A-Za-z0-9_$]*), "
    r"(?P<response>[A-Za-z_$][A-Za-z0-9_$]*)>$"
)
METHOD_PATH_RE = re.compile(
    r'^\[pp\+0x(?P<offset>[0-9A-Fa-f]+)\] String: "'
    r'(?P<path>/[A-Za-z_][A-Za-z0-9_.]*/[A-Za-z_][A-Za-z0-9_]*)"$'
)
IDA_MESSAGE_RE = re.compile(
    r'idaapi\.set_name\(0x[0-9A-Fa-f]+, "'
    r'(?P<namespace>[^"$]+)\$(?P<library>[^"$]+?\.pb)_'
    r'(?P<type>[A-Za-z_$][A-Za-z0-9_$]*)::_i_[0-9A-Fa-f]+"\)'
)
CSV_FIELDS = ("path", "service", "method", "request_fqn", "response_fqn", "pp_offset")


class ExtractionError(ValueError):
    """Raised when binary evidence cannot support one deterministic exact signature."""


@dataclasses.dataclass(frozen=True, order=True)
class MessageSymbol:
    """A generated-message BuilderInfo symbol recovered by blutter."""

    namespace: str
    library: str
    dart_type: str

    @property
    def fqn(self) -> str:
        return f".{self.namespace}.{self.dart_type}"


@dataclasses.dataclass(frozen=True, order=True)
class RawMethod:
    """A method path adjacent to its generated-client type arguments in ``pp.txt``."""

    path: str
    request_type: str
    response_type: str
    pp_offset: int

    @property
    def service(self) -> str:
        return self.path[1:].rsplit("/", 1)[0]

    @property
    def method(self) -> str:
        return self.path.rsplit("/", 1)[1]


@dataclasses.dataclass(frozen=True, order=True)
class GrpcSignature:
    """One gRPC method whose request and response FQNs are fully resolved."""

    path: str
    service: str
    method: str
    request_fqn: str
    response_fqn: str
    pp_offset: int


def parse_pp_methods(path) -> list[RawMethod]:
    """Parse exactly adjacent type-argument/path pairs and reject conflicting duplicates."""
    with open(path, errors="replace") as stream:
        lines = stream.read().splitlines()

    by_path: dict[str, RawMethod] = {}
    for type_line, path_line in zip(lines, lines[1:], strict=False):
        type_match = TYPE_ARGUMENTS_RE.match(type_line)
        path_match = METHOD_PATH_RE.match(path_line)
        if not type_match or not path_match:
            continue
        type_offset = int(type_match.group("offset"), 16)
        path_offset = int(path_match.group("offset"), 16)
        if path_offset != type_offset + 8:
            continue
        method = RawMethod(
            path=path_match.group("path"),
            request_type=type_match.group("request"),
            response_type=type_match.group("response"),
            pp_offset=type_offset,
        )
        previous = by_path.get(method.path)
        if previous and (
            previous.request_type != method.request_type
            or previous.response_type != method.response_type
        ):
            raise ExtractionError(
                f"conflicting type arguments for {method.path}: "
                f"<{previous.request_type}, {previous.response_type}> and "
                f"<{method.request_type}, {method.response_type}>"
            )
        if previous is None or method.pp_offset < previous.pp_offset:
            by_path[method.path] = method
    return sorted(by_path.values(), key=lambda item: item.path)


def parse_message_symbols(path) -> dict[str, tuple[MessageSymbol, ...]]:
    """Index generated-message ``_i`` symbols from blutter's IDA naming script."""
    symbols: defaultdict[str, set[MessageSymbol]] = defaultdict(set)
    with open(path, errors="replace") as stream:
        for line in stream:
            match = IDA_MESSAGE_RE.search(line)
            if not match:
                continue
            symbol = MessageSymbol(
                namespace=match.group("namespace"),
                library=match.group("library"),
                dart_type=match.group("type"),
            )
            symbols[symbol.dart_type].add(symbol)
    return {name: tuple(sorted(entries)) for name, entries in symbols.items()}


def resolve_type(
    short_name: str,
    service: str,
    symbols: dict[str, tuple[MessageSymbol, ...]],
) -> str:
    """Resolve a Dart type to one protobuf FQN, preferring the service namespace."""
    candidates = symbols.get(short_name, ())
    if not candidates:
        raise ExtractionError(f"unresolved generated-message type {short_name!r} for {service}")

    service_namespace = service.rsplit(".", 1)[0]
    local_fqns = {
        candidate.fqn for candidate in candidates if candidate.namespace == service_namespace
    }
    if len(local_fqns) == 1:
        return local_fqns.pop()
    if len(local_fqns) > 1:
        raise ExtractionError(
            f"ambiguous service-local type {short_name!r} for {service}: "
            f"{', '.join(sorted(local_fqns))}"
        )

    fqns = {candidate.fqn for candidate in candidates}
    if len(fqns) == 1:
        return fqns.pop()
    raise ExtractionError(
        f"ambiguous generated-message type {short_name!r} for {service}: {', '.join(sorted(fqns))}"
    )


def extract_signatures(pp_path, add_names_path) -> list[GrpcSignature]:
    """Join pp-table method evidence to exact generated-message FQNs."""
    methods = parse_pp_methods(pp_path)
    symbols = parse_message_symbols(add_names_path)
    signatures = []
    for method in methods:
        signatures.append(
            GrpcSignature(
                path=method.path,
                service=method.service,
                method=method.method,
                request_fqn=resolve_type(method.request_type, method.service, symbols),
                response_fqn=resolve_type(method.response_type, method.service, symbols),
                pp_offset=method.pp_offset,
            )
        )
    return signatures


def write_csv(signatures: list[GrpcSignature], stream) -> None:
    """Write stable, path-sorted CSV without mutating the input evidence."""
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for signature in sorted(signatures, key=lambda item: item.path):
        writer.writerow(
            {
                "path": signature.path,
                "service": signature.service,
                "method": signature.method,
                "request_fqn": signature.request_fqn,
                "response_fqn": signature.response_fqn,
                "pp_offset": f"0x{signature.pp_offset:x}",
            }
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pp", help="path to the blutter dump's pp.txt")
    parser.add_argument("add_names", help="path to ida_script/addNames.py from the same dump")
    args = parser.parse_args(argv)
    try:
        signatures = extract_signatures(args.pp, args.add_names)
    except ExtractionError as exc:
        parser.error(str(exc))
    write_csv(signatures, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
