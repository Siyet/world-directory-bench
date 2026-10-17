"""Download public source snapshots or verify an existing snapshot, using stdlib only."""

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import urllib.request

GEONAMES = "https://download.geonames.org/export/dump/"
# Use the upstream OurAirports repository, not a transformed downstream dataset.
OURAIRPORTS = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/"


def sources(profile):
    files = [
        ("countryInfo.txt", GEONAMES, "CC-BY-4.0"),
        ("admin1CodesASCII.txt", GEONAMES, "CC-BY-4.0"),
        ("admin2Codes.txt", GEONAMES, "CC-BY-4.0"),
        ("readme.txt", GEONAMES, "CC-BY-4.0"),
        ("allCountries.zip" if profile == "full" else "cities500.zip", GEONAMES, "CC-BY-4.0"),
        ("airports.csv", OURAIRPORTS, "Public Domain"),
        ("countries.csv", OURAIRPORTS, "Public Domain"),
        ("regions.csv", OURAIRPORTS, "Public Domain"),
    ]
    return files


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    if type(manifest.get("format")) is not int or manifest["format"] != 1:
        raise ValueError("Unsupported manifest format")
    if manifest.get("profile") not in ("full", "cities500"):
        raise ValueError("Unknown snapshot profile")
    expected = {name: (base + name, license_name) for name, base, license_name in sources(manifest["profile"])}
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) != len(expected):
        raise ValueError("Incomplete snapshot manifest")
    names = [entry.get("name") for entry in entries if isinstance(entry, dict)]
    if len(names) != len(entries) or any(not isinstance(name, str) for name in names):
        raise ValueError("Invalid snapshot entry")
    if len(set(names)) != len(names) or set(names) != set(expected):
        raise ValueError("Unexpected or duplicate snapshot files")
    for entry in manifest["files"]:
        if (entry.get("url"), entry.get("license")) != expected[entry["name"]]:
            raise ValueError("Unexpected snapshot provenance")
        checksum = entry.get("sha256")
        if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError("Invalid snapshot checksum")
        if type(entry.get("bytes")) is not int or entry["bytes"] < 0:
            raise ValueError("Invalid snapshot size")
        if not isinstance(entry.get("resolved_url"), str) or not entry["resolved_url"].startswith("https://"):
            raise ValueError("Invalid resolved URL")
        if not isinstance(entry.get("headers"), dict):
            raise ValueError("Missing response headers")
        downloaded = dt.datetime.fromisoformat(entry["downloaded_at"])
        if downloaded.tzinfo is None:
            raise ValueError("Snapshot timestamp needs timezone")
        path = directory / entry["name"]
        if path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
            raise ValueError(f"Snapshot mismatch: {entry['name']}")
    return manifest


def fetch(directory, profile):
    # A new snapshot must use a new directory. Never silently refresh benchmark input.
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {"format": 1, "profile": profile, "files": []}
    for name, base, license_name in sources(profile):
        url = base + name
        req = urllib.request.Request(url, headers={"User-Agent": "world-directory-benchmark/0.1"})
        path = directory / name
        with urllib.request.urlopen(req, timeout=180) as response, path.open("xb") as out:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                out.write(chunk)
            headers = {key: response.headers.get(key) for key in ("Last-Modified", "ETag")}
            resolved_url = response.url
        manifest["files"].append({
            "name": name, "url": url, "resolved_url": resolved_url,
            "downloaded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "license": license_name, "bytes": path.stat().st_size,
            "sha256": digest(path), "headers": headers,
        })
        print(f"{name}: {path.stat().st_size} bytes", flush=True)
    # An incomplete download has no manifest and cannot pass verification.
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    verify(directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=pathlib.Path)
    parser.add_argument("--profile", choices=("cities500", "full"), default="cities500")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        verify(args.directory)
        print("Snapshot verified")
    else:
        fetch(args.directory, args.profile)
