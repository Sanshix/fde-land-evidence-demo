"""Repack already installed public wheels from uv cache, verifying every RECORD entry."""

import base64
import csv
import email
import hashlib
import zipfile
from pathlib import Path


def repack(cache, destination, requirements):
    cache, destination = Path(cache), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    wanted = {
        line.split("==")[0].replace("-", "_").lower(): line.split("==")[1]
        for line in Path(requirements).read_text().splitlines()
        if line and not line.startswith("#")
    }
    records = []
    for metadata in sorted((cache / "archive-v0").glob("*/*.dist-info/METADATA")):
        m = email.message_from_string(metadata.read_text())
        name = m["Name"].lower().replace("-", "_")
        if name not in wanted or m["Version"] != wanted[name]:
            continue
        root = metadata.parent.parent
        record = metadata.parent / "RECORD"
        rows = list(csv.reader(record.read_text().splitlines()))
        for filename, digest, size in rows:
            f = root / filename
            if not f.resolve().is_relative_to(root.resolve()) or not f.is_file():
                raise ValueError("unsafe or missing RECORD file: " + filename)
            if digest:
                algorithm, expected = digest.split("=", 1)
                actual = (
                    base64.urlsafe_b64encode(hashlib.new(algorithm, f.read_bytes()).digest())
                    .decode()
                    .rstrip("=")
                )
                if actual != expected or (size and len(f.read_bytes()) != int(size)):
                    raise ValueError("RECORD integrity mismatch: " + filename)
        tags = [
            x.split(": ", 1)[1]
            for x in (metadata.parent / "WHEEL").read_text().splitlines()
            if x.startswith("Tag: ")
        ]
        parsed = [t.split("-") for t in tags]
        tag = "-".join(".".join(sorted({t[i] for t in parsed})) for i in range(3))
        target = destination / f"{name}-{m['Version']}-{tag}.whl"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for filename, _, _ in rows:
                z.write(root / filename, filename)
        records.append(
            {
                "package": name,
                "version": m["Version"],
                "verified_record_files": len(rows),
                "archive_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "source": "existing trusted uv wheel cache; repacked archive, original file content",
            }
        )
        del wanted[name]
    if wanted:
        raise ValueError("missing cached dependencies: " + str(wanted))
    return records
