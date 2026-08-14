#!/usr/bin/env python3
"""Serve one track out of a CUE-image album, without downloading or splitting it.

Emby has no CUE support — a rip stored as one FLAC plus a sheet is a single
item of album length however it is filed. `torbox_sync.py` writes one .strm
per track pointing here; this answers each with just that track's audio, cut
out of the remote file by ffmpeg as it streams.

    GET /slice?u=<base64url of the source URL>&ss=<start s>&t=<length s>
    GET /probe?u=…            what ffprobe says about the source
    GET /healthz

The answer is PCM in a WAV wrapper at the source's own rate and depth, so
nothing is re-encoded and nothing is lost. PCM is the point: its length in
bytes follows from its length in seconds, which gives an exact
Content-Length and byte-range seeking — a piped FLAC could give neither, and
Emby would show a track of unknown duration that cannot be scrubbed.

Env: PORT (8099), FFMPEG (ffmpeg), FFPROBE (ffprobe), PROBE_CACHE.
"""
import base64
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "8099"))
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
CACHE = Path(os.environ.get("PROBE_CACHE", "/sync-state/cueslice-probe.json"))
CHUNK = 64 * 1024

_probes = {}
_lock = threading.Lock()


def load_cache():
    try:
        _probes.update(json.loads(CACHE.read_text()))
    except Exception:
        pass


def save_cache():
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(_probes))
    except Exception as e:
        print(f"[warn] cannot write {CACHE}: {e}", file=sys.stderr)


def probe(url):
    """Sample rate, channels, sample format and duration of the source."""
    key = base64.urlsafe_b64encode(url.encode()).decode()[:64]
    with _lock:
        if key in _probes:
            return _probes[key]
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=sample_rate,channels,bits_per_raw_sample,sample_fmt",
         "-show_entries", "format=duration", "-of", "json", url],
        capture_output=True, timeout=120)
    if out.returncode:
        raise RuntimeError(out.stderr.decode()[:300] or "ffprobe failed")
    data = json.loads(out.stdout)
    st = (data.get("streams") or [{}])[0]
    bits = int(st.get("bits_per_raw_sample") or 0)
    if not bits:
        bits = 24 if "32" in (st.get("sample_fmt") or "") else 16
    info = {"rate": int(st.get("sample_rate") or 44100),
            "channels": int(st.get("channels") or 2),
            "bits": 24 if bits > 16 else 16,
            "duration": float((data.get("format") or {}).get("duration") or 0)}
    with _lock:
        _probes[key] = info
        save_cache()
    return info


def wav_header(data_len, rate, channels, bits):
    block = channels * bits // 8
    return (b"RIFF" + (36 + data_len).to_bytes(4, "little") + b"WAVEfmt "
            + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
            + channels.to_bytes(2, "little") + rate.to_bytes(4, "little")
            + (rate * block).to_bytes(4, "little")
            + block.to_bytes(2, "little") + bits.to_bytes(2, "little")
            + b"data" + data_len.to_bytes(4, "little"))


def ffmpeg_pcm(url, start, length, info):
    """Start ffmpeg decoding [start, start+length) of the source as raw PCM."""
    codec = "pcm_s24le" if info["bits"] == 24 else "pcm_s16le"
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-v", "error",
           # A track runs for minutes; the CDN dropping the connection part
           # way through must not end the track
           "-reconnect", "1", "-reconnect_streamed", "1",
           "-reconnect_delay_max", "10",
           "-ss", f"{start:.6f}", "-i", url]
    if length is not None:
        cmd += ["-t", f"{length:.6f}"]
    cmd += ["-vn", "-map", "0:a:0", "-acodec", codec,
            "-ar", str(info["rate"]), "-ac", str(info["channels"]),
            "-f", codec[4:], "-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cueslice/1.0"

    def log_message(self, fmt, *a):        # one line per request is enough
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    def _query(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return {k: v[0] for k, v in q.items()}

    def _fail(self, code, msg):
        body = msg.encode()[:500]
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        route = urllib.parse.urlparse(self.path).path
        if route == "/healthz":
            return self._fail(200, "ok")
        try:
            q = self._query()
            url = base64.urlsafe_b64decode(q["u"]).decode()
        except Exception:
            return self._fail(400, "bad or missing u=")
        if route == "/probe":
            try:
                return self._fail(200, json.dumps(probe(url)))
            except Exception as e:
                return self._fail(502, f"probe failed: {e}")
        if route != "/slice":
            return self._fail(404, "no such route")
        try:
            info = probe(url)
        except Exception as e:
            return self._fail(502, f"probe failed: {e}")
        try:
            start = float(q.get("ss") or 0)
            length = float(q["t"]) if q.get("t") else None
        except ValueError:
            return self._fail(400, "ss and t must be seconds")
        if length is None and info["duration"]:
            length = max(0.0, info["duration"] - start)
        if not length:
            return self._fail(502, "source duration unknown")

        block = info["channels"] * info["bits"] // 8
        frames = int(round(length * info["rate"]))
        data_len = frames * block
        header = wav_header(data_len, info["rate"], info["channels"],
                            info["bits"])
        total = len(header) + data_len

        first, last = 0, total - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            lo, _, hi = rng[6:].partition("-")
            try:
                first = int(lo) if lo else 0
                last = int(hi) if hi else total - 1
            except ValueError:
                first, last = 0, total - 1
            if first >= total:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                return self.end_headers()
        last = min(last, total - 1)
        count = last - first + 1

        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(count))
        if rng:
            self.send_header("Content-Range", f"bytes {first}-{last}/{total}")
        self.end_headers()
        if head_only or self.command == "HEAD":
            return

        sent = 0
        if first < len(header):
            piece = header[first:len(header)][:count]
            self.wfile.write(piece)
            sent += len(piece)
        # Resume mid-PCM by moving the decode window, not by reading and
        # discarding: skip whole frames with -ss, then the odd bytes inside one
        pcm_at = max(0, first - len(header))
        skip = pcm_at % block
        proc = ffmpeg_pcm(url, start + (pcm_at // block) / info["rate"],
                          length - (pcm_at // block) / info["rate"], info)
        try:
            while sent < count:
                buf = proc.stdout.read(min(CHUNK, count - sent + skip))
                if not buf:
                    break
                if skip:
                    buf, skip = buf[skip:], 0
                    if not buf:
                        continue
                buf = buf[:count - sent]
                self.wfile.write(buf)
                sent += len(buf)
            # A short read means the source ran out: pad so the promised
            # Content-Length is met and the client does not hang waiting
            if sent < count:
                pad = b"\x00" * (count - sent)
                self.wfile.write(pad)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.kill()
            proc.stdout.close()
            proc.wait()


def main():
    load_cache()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"cueslice on :{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
