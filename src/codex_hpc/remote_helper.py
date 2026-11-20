"""Self-contained helper bundled beside its matching release metadata."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--describe", action="store_true", required=True,
                        help="Report this helper's release and protocol identity.")
    parser.parse_args()
    release = json.loads(Path(__file__).with_name("release.json").read_text())
    print(json.dumps({key: release[key] for key in (
        "version", "protocol_version", "image_interface_version")}))


if __name__ == "__main__":
    main()
