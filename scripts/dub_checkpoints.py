#!/usr/bin/env python3
"""Prove a downloaded model is the model, before something loads it at 3am.

`hf download` returned 0 having written three corrupt files. Their sizes
matched the remote exactly and nothing in its output suggested a problem; the
damage surfaced twenty minutes later as an unreadable checkpoint in the middle
of a model load, which reads like a bug in the renderer rather than a bad
download. The Xet chunked-transfer backend was the cause, and
`HF_HUB_DISABLE_XET=1` avoided it.

So the bytes are checked against what the hub says they should be, and then
every torch archive is actually opened. Both, because they catch different
things: a checksum catches a bad transfer, and opening the archive catches a
file that is byte-perfect and still unreadable by the torch in this venv.

Both hashes are printed on a mismatch rather than a verdict alone. A check
that only says yes or no is a check nobody can audit when it is wrong, and
this one was wrong once already.

Usage:
    python3 scripts/dub_checkpoints.py dub/checkpoints_2_5
    python3 scripts/dub_checkpoints.py dub/checkpoints_2_5 --repo IndexTeam/IndexTTS-2.5
"""

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

# Read in chunks: these files run to several gigabytes and hashing one whole in
# memory would cost more than the download did.
CHUNK = 1 << 22


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            sha.update(block)
    return sha.hexdigest()


def remote_hashes(repo):
    """The hub's sha256 per file, for the files it stores in LFS.

    Read as an attribute. `lfs` is a BlobLfsInfo dataclass and not the dict it
    looks like, so `.get("sha256")` quietly returns nothing — which then reads
    as "this file has no published checksum" and skips the very comparison the
    tool exists to make. It reported a corrupt checkpoint as ok exactly once
    before this line was written correctly.
    """
    from huggingface_hub import HfApi
    info = HfApi().model_info(repo, files_metadata=True)
    return {s.rfilename: getattr(s.lfs, "sha256", None)
            for s in info.siblings if s.lfs}


def readable(path):
    """Whether torch can actually open this archive.

    A checkpoint can match its checksum and still not load — a container the
    installed torch cannot read is not a corrupt download, and the two want
    different answers, so they are reported apart.
    """
    if path.suffix not in (".pth", ".pt", ".bin"):
        return True, ""
    if not zipfile.is_zipfile(path):
        return True, ""                       # a plain pickle, not a zip archive
    try:
        broken = zipfile.ZipFile(path).testzip()
    except Exception as error:                # noqa: BLE001 - reported, not raised
        return False, f"{type(error).__name__}: {error}"
    return (broken is None), (f"first bad member {broken}" if broken else "")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory")
    parser.add_argument("--repo", default="IndexTeam/IndexTTS-2.5",
                        help="the hub repository these files came from")
    parser.add_argument("--no-remote", action="store_true",
                        help="skip the checksum pass and only open the archives")
    args = parser.parse_args()

    root = Path(args.directory)
    if not root.is_dir():
        raise SystemExit(f"{root} is not a directory")

    expected = {} if args.no_remote else remote_hashes(args.repo)
    if not args.no_remote and not expected:
        raise SystemExit(f"{args.repo} published no checksums; refusing to report "
                         f"on files this cannot actually check")
    files = sorted(p for p in root.rglob("*") if p.is_file() and ".cache" not in p.parts)
    wrong = []

    for path in files:
        name = str(path.relative_to(root))
        want = expected.get(name)
        size = path.stat().st_size

        if want:
            got = digest(path)
            if got != want:
                wrong.append(name)
                print(f"  BAD BYTES  {name}  ({size / 1e6:.0f} MB)")
                print(f"             here   {got}")
                print(f"             hub    {want}")
                continue

        opens, why = readable(path)
        if not opens:
            wrong.append(name)
            print(f"  UNREADABLE {name}  ({size / 1e6:.0f} MB)  {why}")
            print(f"             bytes match the hub, so this is not the download")
        elif want:
            print(f"  ok         {name}  ({size / 1e6:.0f} MB)")

    unchecked = [str(p.relative_to(root)) for p in files
                 if not expected.get(str(p.relative_to(root)))]
    print(f"\n{len(files)} files, {len(files) - len(unchecked)} checksummed, "
          f"{len(wrong)} bad")
    if unchecked and not args.no_remote:
        print(f"{len(unchecked)} carry no hub checksum (small files are not stored "
              f"in LFS); their archives were still opened")
    if wrong:
        print("\nre-fetch with the chunked transfer off, which is what caused this "
              "before:\n  HF_HUB_DISABLE_XET=1 hf download <repo> <file> "
              "--local-dir <dir> --force-download")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
