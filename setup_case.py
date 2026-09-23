"""Restore participant CSVs from the versioned archive without network access."""
import hashlib
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "vendor" / "beeline_case_participants.zip"
ARCHIVE_SHA256 = "df1d955fb97816ff6de8ceb142ed589915d969f43f840a650dcdfbe12734f20b"
DATA_FILES = (
    "customer_profile.csv", "tariff_dictionary.csv", "feature_dictionary.csv",
    "data/change_tariff.csv", "data/traffic.csv", "data/arpu_monthly.csv", "data/dict_tariff.csv",
)


def restore(root=ROOT, archive=ARCHIVE):
    root, archive = Path(root).resolve(), Path(archive).resolve()
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Participant archive checksum mismatch; extraction cancelled")
    restored, unchanged = [], []
    with zipfile.ZipFile(archive) as source:
        for name in DATA_FILES:
            destination = (root / name).resolve()
            if not destination.is_relative_to(root):
                raise ValueError("Archive destination is outside the project")
            content = source.read(name)
            if destination.exists():
                if destination.read_bytes() != content:
                    raise FileExistsError(f"Refusing to overwrite modified data: {destination}")
                unchanged.append(name)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            restored.append(name)
    return restored, unchanged


if __name__ == "__main__":
    created, existing = restore()
    print(f"Participant CSVs ready: restored={len(created)}, unchanged={len(existing)}")
