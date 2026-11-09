#!/usr/bin/env python3
"""Turns the SDK parameter into a hash-locked requirements file for pip.

The parameter holds the SDK release the build installs, as JSON:

    {"version": "0.1.3", "filename": "pippa_agentapp_sdk-0.1.3-py3-none-any.whl",
     "sha256": "<hex>", ...}

The output file pins that exact version with that hash, so pip with
--require-hashes installs nothing else. Prints the pinned requirement.

Exit codes:
    0  file written
    2  the parameter is missing, unreadable or inconsistent

Usage:
    sdk_pin.py --parameter-file FILE --out FILE [--package pippa-agentapp-sdk]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SDK_PACKAGE = "pippa-agentapp-sdk"
#: Final releases only, shared by every SDK component: no epoch, prefix, local, pre or dev part.
VERSION_PATTERN = re.compile(r"^\d+(\.\d+){0,3}(\.post\d+)?$", re.ASCII)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PinError(Exception):
    """The parameter cannot be turned into a pin."""


@dataclass(frozen=True)
class SdkPin:
    package: str
    version: str
    sha256: str

    @property
    def requirement(self) -> str:
        return f"{self.package}=={self.version}"

    def render(self) -> str:
        return f"{self.requirement} --hash=sha256:{self.sha256}\n"


class PinSource(ABC):
    @abstractmethod
    def load(self) -> dict[str, Any]:
        """Returns the decoded parameter value."""


class ParameterFileSource(PinSource):
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PinError(f"parameter file could not be read: {exc}") from exc
        except ValueError as exc:
            raise PinError(f"parameter value is not JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise PinError("parameter value is not a JSON object")
        return value


class PinBuilder:
    def __init__(self, package: str) -> None:
        self.package = package
        self.distribution = re.sub(r"[-_.]+", "_", package.lower())

    def build(self, value: dict[str, Any]) -> SdkPin:
        version = self._field(value, "version")
        sha256 = self._field(value, "sha256").lower()
        filename = self._field(value, "filename")
        if not VERSION_PATTERN.fullmatch(version):
            raise PinError(f"version {version!r} is not a release version")
        if not SHA256_PATTERN.fullmatch(sha256):
            raise PinError("sha256 is not 64 hexadecimal characters")
        expected = f"{self.distribution}-{version}-"
        if not (filename.lower().startswith(expected) and filename.lower().endswith(".whl")):
            raise PinError(f"filename {filename!r} is not a wheel of {self.package} {version}")
        return SdkPin(self.package, version, sha256)

    @staticmethod
    def _field(value: dict[str, Any], name: str) -> str:
        field = value.get(name)
        if not isinstance(field, str) or not field.strip():
            raise PinError(f"parameter has no {name}")
        return field.strip()


def pin_source(args: argparse.Namespace) -> PinSource:
    return ParameterFileSource(args.parameter_file)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write the hash-locked requirement of the Pippa SDK")
    parser.add_argument("--parameter-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--package", default=SDK_PACKAGE, help="Name of the Pippa SDK distribution")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        pin = PinBuilder(args.package).build(pin_source(args).load())
        args.out.write_text(pin.render(), encoding="utf-8")
    except PinError as exc:
        print(f"SDK parameter rejected: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"SDK pin could not be written: {exc}", file=sys.stderr)
        return 2
    print(pin.requirement)
    return 0


if __name__ == "__main__":
    sys.exit(main())
