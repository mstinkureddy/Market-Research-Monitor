"""Verify a single-file GitHub artifact before reading its report bytes."""
from dataclasses import dataclass, replace
from hashlib import sha256
from hmac import compare_digest
from io import BytesIO
import re
import stat
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile
from zlib import error as ZipError

from .batch import REPOSITORY
from .report import ARTIFACT_LIMIT, Provenance, Review, review, withheld

NAME = "top10-synthetic-research"
MEMBER = "report.json"
ARCHIVE_LIMIT = 128 * 1024


@dataclass(frozen=True)
class Archive:
    """Metadata obtained from the authorized GitHub Actions artifact listing."""
    repository: str
    name: str
    run_id: str
    head_sha: str
    digest: str | None
    expired: bool


def review_archive(data: bytes | None, metadata: Archive | None, proof: Provenance, now) -> Review:
    if data is None:
        return review(None, proof, now)
    if (metadata is None or metadata.repository != REPOSITORY or metadata.name != NAME
            or metadata.run_id != proof.run.run_id or metadata.head_sha != proof.run.commit
            or metadata.expired is not False or not metadata.digest or len(data) > ARCHIVE_LIMIT
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", metadata.digest)
            or not compare_digest(metadata.digest, "sha256:" + sha256(data).hexdigest())):
        return withheld("UNVERIFIED", "Artifact identity, retention or SHA-256 unverified.")
    try:
        with ZipFile(BytesIO(data)) as archive:
            files = archive.infolist()
            if len(files) != 1:
                raise ValueError
            file = files[0]
            mode = stat.S_IFMT(file.external_attr >> 16)
            if (file.filename != MEMBER or file.orig_filename != MEMBER or file.is_dir() or file.flag_bits & 1
                    or mode not in (0, stat.S_IFREG) or not 0 < file.file_size <= ARTIFACT_LIMIT
                    or file.compress_type not in (ZIP_STORED, ZIP_DEFLATED)):
                raise ValueError
            with archive.open(file) as member:
                payload = member.read(ARTIFACT_LIMIT + 1)
            if len(payload) != file.file_size:
                raise ValueError
    except (BadZipFile, NotImplementedError, RuntimeError, ValueError, EOFError, OSError, ZipError):
        return withheld("UNVERIFIED", "Artifact archive invalid.")
    return review(payload, replace(proof, artifact_verified=True), now)
