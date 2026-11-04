"""AnyConverter - convert audio, video, images, PDFs, documents, spreadsheets,
subtitles and archives into the format you want.

Python + tkinter. Audio, video and subtitles go through FFmpeg; documents,
slides and spreadsheets through LibreOffice (both are offered for install when
missing). Images, PDFs and data files need a few Python packages, which the
built AnyConverter.exe already contains:
      pip install pillow pillow-heif pypdfium2 openpyxl

Run:  py anyconverter.py         (Windows)
      python3 anyconverter.py    (Linux; needs the python3-tk package)
"""

import csv
import importlib.util
import io
import json
import os
import platform
import queue
import tarfile
import tempfile
import traceback
import urllib.request
import uuid
import zipfile
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import re
import shutil
import subprocess
import sys
import threading

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    sys.exit("AnyConverter needs tkinter. On Linux install it with e.g.\n"
             "  sudo apt install python3-tk     (Debian/Ubuntu/Mint)\n"
             "  sudo dnf install python3-tkinter (Fedora)\n"
             "  sudo pacman -S tk                (Arch)")

IS_WIN = os.name == "nt"
VERSION = "1.0"        # goes into bug reports

# ---------------------------------------------------------------- formats ---

QUALITY = {
    "High":   {"crf": 18, "vp9crf": 24, "theora": 9, "abr": "320k", "ogq": "8"},
    "Medium": {"crf": 23, "vp9crf": 32, "theora": 7, "abr": "192k", "ogq": "5"},
    "Small":  {"crf": 28, "vp9crf": 40, "theora": 5, "abr": "128k", "ogq": "3"},
}

# ext -> (video encoder, audio encoder); None = drop that stream
TARGETS = {
    "mp4": ("h264", "aac"), "mkv": ("h264", "aac"), "mov": ("h264", "aac"),
    "m4v": ("h264", "aac"), "flv": ("h264", "aac"), "webm": ("vp9", "opus"),
    "ogv": ("theora", "vorbis"), "avi": ("mpeg4", "mp3"), "wmv": ("wmv2", "wma"),
    "gif": ("gif", None),
    "mp3": (None, "mp3"), "wav": (None, "pcm_s16le"), "flac": (None, "flac"),
    "m4a": (None, "aac"), "aac": (None, "aac"), "ogg": (None, "vorbis"),
    "opus": (None, "opus"), "wma": (None, "wma"), "aiff": (None, "pcm_s16be"),
}

# Streams already in one of these codecs are copied as-is instead of re-encoded:
# no quality loss and close to instant. (Skipped on "Small", which asks to shrink.)
_MP4_V = {"h264", "hevc", "av1", "mpeg4"}
_MP4_A = {"aac", "mp3", "ac3", "eac3", "alac", "opus"}
COPY_VIDEO = {
    "mp4": _MP4_V, "m4v": _MP4_V, "mov": _MP4_V | {"prores", "mjpeg"},
    "mkv": _MP4_V | {"vp8", "vp9", "theora", "mpeg2video", "prores", "mjpeg"},
    "webm": {"vp8", "vp9", "av1"}, "ogv": {"theora"}, "avi": {"mpeg4", "mjpeg"},
    "flv": {"h264"}, "wmv": {"wmv1", "wmv2"},
}
COPY_AUDIO = {
    "mp4": _MP4_A, "m4v": _MP4_A, "mov": _MP4_A | {"pcm_s16le", "pcm_s24le"},
    "mkv": _MP4_A | {"vorbis", "flac", "dts", "truehd", "pcm_s16le", "pcm_s24le"},
    "webm": {"opus", "vorbis"}, "ogv": {"vorbis", "opus", "flac"}, "avi": {"mp3", "ac3"},
    "flv": {"aac", "mp3"}, "wmv": {"wmav1", "wmav2"},
    "mp3": {"mp3"}, "wav": {"pcm_s16le"}, "flac": {"flac"}, "m4a": {"aac", "alac"},
    "aac": {"aac"}, "ogg": {"vorbis", "opus", "flac"}, "opus": {"opus"},
    "wma": {"wmav1", "wmav2"}, "aiff": {"pcm_s16be"},
}


def video_encoder_args(enc, q):
    p = QUALITY[q]
    if enc == "h264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(p["crf"]),
                "-pix_fmt", "yuv420p"]
    if enc == "vp9":
        return ["-c:v", "libvpx-vp9", "-crf", str(p["vp9crf"]), "-b:v", "0",
                "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
                "-tile-columns", "2", "-threads", str(os.cpu_count() or 4)]
    if enc == "theora":
        return ["-c:v", "libtheora", "-q:v", str(p["theora"])]
    if enc == "mpeg4":
        return ["-c:v", "mpeg4", "-q:v", {"High": "2", "Medium": "5", "Small": "9"}[q]]
    if enc == "wmv2":
        return ["-c:v", "wmv2", "-q:v", {"High": "2", "Medium": "5", "Small": "9"}[q]]
    if enc == "gif":
        width = {"High": 720, "Medium": 480, "Small": 320}[q]
        return ["-vf", f"fps=15,scale={width}:-1:flags=lanczos,split[a][b];"
                       f"[a]palettegen[p];[b][p]paletteuse"]
    raise ValueError(enc)


def audio_encoder_args(enc, q):
    p = QUALITY[q]
    rich = p["abr"] if q != "High" else "256k"
    return {
        "aac":       ["-c:a", "aac", "-b:a", rich],
        "mp3":       ["-c:a", "libmp3lame", "-b:a", p["abr"]],
        "vorbis":    ["-c:a", "libvorbis", "-q:a", p["ogq"]],
        "opus":      ["-c:a", "libopus", "-b:a", {"High": "192k", "Medium": "128k", "Small": "64k"}[q]],
        "wma":       ["-c:a", "wmav2", "-b:a", rich],
        "flac":      ["-c:a", "flac"],
        "pcm_s16le": ["-c:a", "pcm_s16le"],
        "pcm_s16be": ["-c:a", "pcm_s16be"],
    }[enc]


def scale_filter(max_size):
    """Down to at most max_size (never up), the picture's shape kept, even sides (encoders want them)."""
    w, h = max_size
    return (f"scale='min({w},iw)':'min({h},ih)':force_original_aspect_ratio=decrease"
            f":force_divisible_by=2")


def build_args(ext, q, info, copy="va", max_size=None):
    """FFmpeg output args for one file. info = probe_info() result.
    copy: which streams may be copied as-is ("va", "v" or "").
    max_size: (width, height) the video may be at most (scaled down, shape kept), or None."""
    venc, aenc = TARGETS[ext]
    vcopy = "v" in copy and q != "Small" and max_size is None
    acopy = "a" in copy and q != "Small"
    args = ["-sn", "-dn"]
    if venc is None:
        args += ["-vn"]
    elif vcopy and info.get("vcodec") in COPY_VIDEO.get(ext, ()):
        args += ["-c:v", "copy"]
        if info["vcodec"] == "hevc" and ext in ("mp4", "m4v", "mov"):
            args += ["-tag:v", "hvc1"]       # so Apple players accept it
    else:
        if max_size:
            args += ["-vf", scale_filter(max_size)]
        args += video_encoder_args(venc, q)
    if aenc is None:
        args += ["-an"]
    elif acopy and info.get("acodec") in COPY_AUDIO.get(ext, ()):
        args += ["-c:a", "copy"]
    else:
        args += audio_encoder_args(aenc, q)
    if ext in ("mp4", "m4v", "mov"):
        args += ["-movflags", "+faststart"]
    return args


# Encoders that can't keep a modern CPU busy on their own: the video is split
# into time slices encoded side by side. Value = threads each slice uses well.
SPLIT_ENCODERS = {"libtheora": 1, "libvpx-vp9": 2}

VIDEO_FORMATS = ["mp4", "mkv", "mov", "webm", "ogv", "avi", "m4v", "flv", "wmv"]
AUDIO_FORMATS = ["mp3", "wav", "flac", "m4a", "aac", "ogg", "opus", "wma", "aiff"]
IMAGE_FORMATS = ["png", "jpg", "webp", "avif", "gif", "bmp", "tiff", "ico"]

# Every format AnyConverter can write, grouped the way the "Convert to" list shows them.
FORMAT_GROUPS = [
    ("video", VIDEO_FORMATS),
    ("audio", AUDIO_FORMATS),
    ("image", IMAGE_FORMATS),
    ("document", ["pdf", "docx", "odt", "rtf", "txt", "html"]),
    ("slides", ["pptx", "odp"]),
    ("spreadsheet", ["xlsx", "ods", "csv", "tsv", "json"]),
    ("subtitles", ["srt", "vtt", "ass"]),
    ("archive", ["zip", "tar.gz", "tar.xz", "tar"]),
]

# kind -> (extensions read as that kind, formats it can become)
KINDS = {
    "video": ("mp4 mkv mov webm avi m4v flv wmv mpg mpeg 3gp ts mts m2ts vob ogv gif",
              VIDEO_FORMATS + AUDIO_FORMATS + ["gif"]),
    "audio": ("mp3 wav flac m4a aac ogg oga opus wma aiff aif alac amr ac3 dts mka caf",
              AUDIO_FORMATS),
    "image": ("png jpg jpeg jfif webp avif bmp tif tiff ico heic heif tga psd",
              IMAGE_FORMATS + ["pdf"]),
    "pdf": ("pdf", ["png", "jpg", "webp", "tiff", "txt", "pdf"]),   # pdf: merge only
    "document": ("docx doc odt rtf txt html htm", ["pdf", "docx", "odt", "rtf", "txt", "html"]),
    "slides": ("pptx ppt odp", ["pdf", "pptx", "odp"]),
    "spreadsheet": ("xlsx xls ods", ["pdf", "xlsx", "ods", "csv"]),
    "data": ("csv tsv json", ["csv", "tsv", "json", "xlsx"]),
    "subtitles": ("srt vtt ass ssa", ["srt", "vtt", "ass"]),
    "archive": ("zip tar tar.gz tgz tar.xz txz tar.bz2 tbz2", ["zip", "tar.gz", "tar.xz", "tar"]),
}
KIND_OF = {e: k for k, (exts, _) in KINDS.items() for e in exts.split()}
# Spellings of the same format, so png -> png isn't offered and jpeg -> jpg is.
SAME = {"jpeg": "jpg", "jfif": "jpg", "tif": "tiff", "heif": "heic", "htm": "html",
        "tgz": "tar.gz", "txz": "tar.xz", "tbz2": "tar.bz2", "aif": "aiff", "oga": "ogg"}
COMPOUND_EXTS = ("tar.gz", "tar.xz", "tar.bz2")


def split_ext(path):
    """("holiday", "tar.gz") for .../holiday.tar.gz; extension in lower case."""
    base = os.path.basename(path)
    for c in COMPOUND_EXTS:
        if base.lower().endswith("." + c) and len(base) > len(c) + 1:
            return base[:-len(c) - 1], c
    stem, e = os.path.splitext(base)
    return stem, e[1:].lower()


def outputs_for(path):
    """Formats this file can be converted to. Unknown files are tried as media."""
    ext = split_ext(path)[1]
    kind = KIND_OF.get(ext, "video")
    outs = list(KINDS[kind][1])
    if ext == "gif":            # animations can also become still or animated images
        outs = VIDEO_FORMATS + [f for f in IMAGE_FORMATS if f != "gif"]
    elif ext == "xlsx":
        outs = ["pdf", "ods", "csv", "tsv", "json"]
    own = SAME.get(ext, ext)
    return [f for f in outs if f != own or f == "pdf"]


def engine_for(path, target):
    """Which converter turns this file into target, or None if none can."""
    if target not in outputs_for(path):
        return None
    ext = split_ext(path)[1]
    kind = KIND_OF.get(ext, "video")
    if kind == "image" or (ext == "gif" and target in IMAGE_FORMATS):
        return "image"
    if kind in ("video", "audio", "subtitles"):
        return "ffmpeg"
    if kind in ("pdf", "archive", "data"):
        return kind
    if ext == "xlsx" and target in ("csv", "tsv", "json"):
        return "data"
    return "office"


def _patterns(exts):
    pats = " ".join(f"*.{e}" for e in exts)
    if os.name != "nt":   # Linux file names are case-sensitive: also match VIDEO.MP4
        pats += " " + pats.upper()
    return pats


FILE_TYPES = [
    ("All supported", _patterns(KIND_OF)),
    ("Audio & video", _patterns(KINDS["video"][0].split() + KINDS["audio"][0].split())),
    ("Images", _patterns(KINDS["image"][0].split())),
    ("PDF", _patterns(["pdf"])),
    ("Documents & slides", _patterns(KINDS["document"][0].split() + KINDS["slides"][0].split())),
    ("Spreadsheets & data", _patterns(KINDS["spreadsheet"][0].split() + KINDS["data"][0].split())),
    ("Subtitles", _patterns(KINDS["subtitles"][0].split())),
    ("Archives", _patterns(KINDS["archive"][0].split())),
    ("All files", "*.*"),
]

# Python packages each converter needs (import name -> pip name)
PACKAGES = {"image": {"PIL": "pillow"}, "pdf": {"PIL": "pillow", "pypdfium2": "pypdfium2"}}

# ----------------------------------------------------------------- ffmpeg ---


_LO = "libreoffice-writer libreoffice-calc libreoffice-impress"

# Outside programs AnyConverter can install for the user.
# linux: package manager -> command that installs the program
TOOLS = {
    "ffmpeg": {
        "name": "FFmpeg", "purpose": "converting audio, video and subtitles",
        # --source winget: a broken Microsoft Store source (certificate errors) would
        # otherwise make winget stop and ask which source to use
        "winget": "winget install -e --id Gyan.FFmpeg --source winget",
        "url": "https://ffmpeg.org/download.html",
        "linux": [
            ("apt-get", "sudo apt install -y ffmpeg"),
            ("dnf", "sudo dnf install -y ffmpeg-free"),
            ("pacman", "sudo pacman -S --needed ffmpeg"),
            ("zypper", "sudo zypper install -y ffmpeg"),
            ("xbps-install", "sudo xbps-install -S ffmpeg"),
            ("apk", "sudo apk add ffmpeg"),
            ("eopkg", "sudo eopkg install ffmpeg"),
        ],
    },
    "office": {
        "name": "LibreOffice", "purpose": "converting documents, slides and spreadsheets",
        "winget": "winget install -e --id TheDocumentFoundation.LibreOffice --source winget",
        "url": "https://www.libreoffice.org/download/download/",
        "linux": [
            ("apt-get", f"sudo apt install -y {_LO}"),
            ("dnf", f"sudo dnf install -y {_LO}"),
            ("pacman", "sudo pacman -S --needed libreoffice-fresh"),
            ("zypper", f"sudo zypper install -y {_LO}"),
            ("xbps-install", "sudo xbps-install -S libreoffice"),
            ("apk", "sudo apk add libreoffice"),
            ("eopkg", "sudo eopkg install libreoffice"),
        ],
    },
}

# terminal emulator -> args placed before the command to run in it
LINUX_TERMINALS = [
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("mate-terminal", ["-x"]),
    ("tilix", ["-e"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("xterm", ["-e"]),
]


def install_command(tool):
    """The one-line command that installs this tool on this system, or None."""
    if IS_WIN:
        return TOOLS[tool]["winget"] if shutil.which("winget", path=_fresh_path()) else None
    for manager, cmd in TOOLS[tool]["linux"]:
        if shutil.which(manager):
            return cmd
    return None


def open_in_terminal(cmd):
    """Run cmd in a visible terminal window so the user can watch it and type
    their password / answer prompts. Returns False if no terminal was found."""
    if IS_WIN:
        subprocess.Popen(["cmd", "/k", cmd], creationflags=subprocess.CREATE_NEW_CONSOLE)
        return True
    script = f"{cmd}; echo; read -p 'Done. Press Enter to close this window.' _"
    for term, prefix in LINUX_TERMINALS:
        if shutil.which(term):
            subprocess.Popen([term, *prefix, "bash", "-c", script],
                             start_new_session=True)
            return True
    return False


def _fresh_path():
    """PATH as it is *now* in the registry, so a just-finished winget install
    is picked up without restarting the app."""
    parts = [os.environ.get("PATH", "")]
    if os.name == "nt":
        import winreg
        keys = [(winreg.HKEY_CURRENT_USER, r"Environment"),
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")]
        for root, sub in keys:
            try:
                with winreg.OpenKey(root, sub) as k:
                    parts.append(os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]))
            except OSError:
                pass
    return os.pathsep.join(parts)


def find_tool(name):
    here = os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, "frozen", False) else sys.argv[0]))
    for cand in (os.path.join(here, name + ".exe"), os.path.join(here, name)):
        if os.path.isfile(cand):
            return cand
    found = shutil.which(name, path=_fresh_path())
    if found:
        return found
    # winget's default install location, in case PATH wasn't updated
    if os.name == "nt":
        import glob
        winget = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet")
        for pattern in (os.path.join(winget, "Links", name + ".exe"),
                        os.path.join(winget, "Packages", "Gyan.FFmpeg*", "*", "bin", name + ".exe")):
            hits = glob.glob(pattern)
            if hits:
                return hits[0]
    return None


def detect_ffmpeg():
    global FFMPEG, FFPROBE
    FFMPEG = find_tool("ffmpeg")
    FFPROBE = find_tool("ffprobe")
    return FFMPEG is not None


def detect_office():
    """LibreOffice's command-line converter (soffice.com on Windows waits for the result)."""
    global SOFFICE
    SOFFICE = None
    if IS_WIN:
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            for exe in ("soffice.com", "soffice.exe"):
                cand = os.path.join(os.environ.get(var, ""), "LibreOffice", "program", exe)
                if os.environ.get(var) and os.path.isfile(cand):
                    SOFFICE = cand
                    return True
    SOFFICE = find_tool("soffice") or find_tool("libreoffice")
    return SOFFICE is not None


DETECT = {"ffmpeg": detect_ffmpeg, "office": detect_office}
FFMPEG = FFPROBE = SOFFICE = None
detect_ffmpeg()
detect_office()
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def probe_info(path):
    """{'duration': secs|None, 'vcodec': name|None, 'acodec': name|None,
        'rate': nominal frame rate as FFmpeg writes it ("24/1", "5000000/83333")|None,
        'fps': that rate as a float|None,
        'vstart' / 'astart': secs the video / audio starts after the file's start}"""
    info = {"duration": None, "vcodec": None, "acodec": None, "rate": None, "fps": None,
            "vstart": 0.0, "astart": 0.0}
    if not FFPROBE:
        return info
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries",
             "format=duration,start_time:stream=codec_type,codec_name,r_frame_rate,"
             "avg_frame_rate,start_time"
             ":stream_disposition=attached_pic",
             "-of", "json", path],
            capture_output=True, text=True, creationflags=NO_WINDOW, timeout=30,
        ).stdout
        data = json.loads(out or "{}")
        dur = data.get("format", {}).get("duration")
        info["duration"] = float(dur) if dur not in (None, "N/A") and float(dur) > 0 else None
        for s in data.get("streams", []):
            if s.get("disposition", {}).get("attached_pic"):
                continue                     # album cover, not a real video
            key = {"video": "vcodec", "audio": "acodec"}.get(s.get("codec_type"))
            if key and not info[key]:
                info[key] = s.get("codec_name")
                if key == "vcodec":
                    # Nominal rate first: it's what constant-rate outputs (Ogg,
                    # WebM...) get, also for game clips with uneven frame timing.
                    for field in ("r_frame_rate", "avg_frame_rate"):
                        num, _, den = (s.get(field) or "0/0").partition("/")
                        if num.isdigit() and den.isdigit() and int(den) \
                                and 0 < int(num) / int(den) <= 240:
                            info["rate"], info["fps"] = f"{num}/{den}", int(num) / int(den)
                            break
                try:
                    info[key[0] + "start"] = max(0.0, float(s["start_time"])
                                                 - float(data["format"]["start_time"]))
                except (KeyError, ValueError):
                    pass
    except Exception:
        pass
    return info


# ------------------------------------------------------ other converters ---

IMAGE_QUALITY = {"High": 95, "Medium": 85, "Small": 70}
PDF_DPI = {"High": 300, "Medium": 150, "Small": 96}       # PDF pages -> images
PIL_FORMAT = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP", "avif": "AVIF", "gif": "GIF",
              "bmp": "BMP", "tiff": "TIFF", "ico": "ICO", "pdf": "PDF"}
PDFIUM_LOCK = threading.Lock()
OFFICE_LOCK = threading.Lock()
OFFICE_PROFILE = os.path.join(tempfile.gettempdir(), "anyconverter-libreoffice")
OFFICE_FILTERS = {"txt": "txt:Text (encoded):UTF8",
                  "csv": "csv:Text - txt - csv (StarCalc):44,34,76,1"}   # comma, UTF-8


def kill_tree(proc):
    """Stop a process and everything it started (soffice leaves soffice.bin
    running otherwise, which blocks every later document conversion)."""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=NO_WINDOW)
        else:
            os.killpg(proc.pid, 15)
    except (OSError, ProcessLookupError):
        pass
    if proc.poll() is None:
        proc.terminate()


def _path_uri(path):
    import pathlib
    return pathlib.Path(os.path.abspath(path)).as_uri()


_heif_done = False


def _heif():
    """Let Pillow open iPhone photos (.heic), if pillow-heif is installed."""
    global _heif_done
    if not _heif_done:
        _heif_done = True
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass


def _flatten(img):
    """RGB on a white background, for formats without transparency."""
    from PIL import Image
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.getchannel("A"))
        return bg
    return img if img.mode == "RGB" else img.convert("RGB")


def _has_alpha(img):
    return img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info


def save_image(img, dst, ext, quality):
    """Save one Pillow image as ext. dst is a path or a file object."""
    q = IMAGE_QUALITY[quality]
    kw = {}
    if ext in ("jpg", "pdf", "bmp"):
        img = _flatten(img)
    elif img.mode not in ("RGB", "RGBA", "L", "LA", "P", "1") or ext in ("webp", "avif", "ico"):
        img = img.convert("RGBA" if _has_alpha(img) else "RGB")
    if ext in ("jpg", "webp", "avif"):
        kw["quality"] = q
        exif = img.getexif()
        if exif:
            kw["exif"] = exif
    if ext == "jpg":
        kw.update(optimize=True, progressive=True)
    elif ext == "webp":
        kw["method"] = 4
    elif ext == "avif":
        from PIL import features
        if not features.check("avif"):
            raise RuntimeError("This copy of Pillow can't write AVIF. Update it: pip install -U pillow")
        kw["speed"] = 6
    elif ext == "tiff":
        kw["compression"] = "tiff_lzw"
    elif ext == "ico":
        kw["sizes"] = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256) if s <= max(img.size)] \
            or [(16, 16)]
    elif ext == "pdf":
        # page about 11 inches on the long side, like a printed page
        kw.update(resolution=max(72.0, max(img.size) / 11), quality=q)
    if "icc_profile" in img.info and ext in ("jpg", "png", "webp", "tiff", "avif"):
        kw["icc_profile"] = img.info["icc_profile"]
    img.save(dst, PIL_FORMAT[ext], **kw)


_NUMBER = re.compile(r"-?(0|[1-9]\d{0,14})(\.\d+)?")


def _typed(v):
    """'42' -> 42 and '1.5' -> 1.5 (but not '007' or phone numbers), other values as-is."""
    if isinstance(v, str) and _NUMBER.fullmatch(v.strip()):
        v = v.strip()
        return float(v) if "." in v else int(v)
    return v


def _text(v):
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def read_table(src):
    """A table as a list of rows; row 0 is the header."""
    ext = split_ext(src)[1]
    if ext == "xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
        try:
            rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
        finally:
            wb.close()
        for r in rows:
            while r and r[-1] is None:
                r.pop()
        while rows and not rows[-1]:
            rows.pop()
        width = max((len(r) for r in rows), default=0)
        return [r + [None] * (width - len(r)) for r in rows]
    raw = open(src, "rb").read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            pass
    if ext == "json":
        data = json.loads(text)
        if isinstance(data, dict):      # {"items": [...]} -> the list inside
            lists = [v for v in data.values() if isinstance(v, list)]
            data = lists[0] if len(data) == 1 and lists else [data]
        if not isinstance(data, list):
            data = [data]
        if data and all(isinstance(d, dict) for d in data):
            header = list(dict.fromkeys(k for d in data for k in d))
            return [header] + [[_text(d.get(k)) for k in header] for d in data]
        if data and all(isinstance(d, list) for d in data):
            return [[_text(v) for v in r] for r in data]
        return [["value"]] + [[_text(v)] for v in data]
    if ext == "tsv":
        delim = "\t"
    else:
        try:
            delim = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|").delimiter
        except csv.Error:
            delim = ","
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim)]


def write_table(rows, dst, ext):
    if ext in ("csv", "tsv"):
        # utf-8-sig so Excel shows accents and umlauts correctly
        with open(dst, "w", encoding="utf-8-sig" if ext == "csv" else "utf-8", newline="") as f:
            w = csv.writer(f, delimiter="," if ext == "csv" else "\t")
            w.writerows([[_text(v) for v in r] for r in rows])
    elif ext == "json":
        header = [str(h) if h not in (None, "") else f"column {i + 1}"
                  for i, h in enumerate(rows[0] if rows else [])]
        records = [{h: (v.isoformat() if hasattr(v, "isoformat") else _typed(v))
                    for h, v in zip(header, r)} for r in rows[1:]]
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
            f.write("\n")
    elif ext == "xlsx":
        import openpyxl
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font
        wb = openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet("Sheet1")
        for k, r in enumerate(rows):
            if k == 0:
                cells = []
                for v in r:
                    c = WriteOnlyCell(ws, value=_text(v))
                    c.font = Font(bold=True)
                    cells.append(c)
                ws.append(cells)
            else:
                ws.append([_typed(v) for v in r])
        wb.save(dst)
    else:
        raise ValueError(ext)


# ------------------------------------------------------------ bug reports ---

# A report becomes a GitHub Issue in this repo (title "<category> (<version>)", label
# "bug", the log folded away). If that fails (no internet) it waits in WAITING_REPORTS
# and goes on the next start.
ISSUES_URL = "https://api.github.com/repos/maxlvl01/AnyConverter/issues"
TOKEN_FILE = "report-token.txt"     # not in git: only the builds carry it (see CLAUDE.md)
WAITING_REPORTS = os.path.join(
    os.environ.get("LOCALAPPDATA", tempfile.gettempdir()) if IS_WIN
    else os.path.expanduser("~/.local/share"), "anyconverter", "bug-reports")
LOG_LINES = 200
REPORT_TEXT_MAX = 20_000    # what the user wrote
ISSUE_BODY_MAX = 60_000     # GitHub takes 65,536 characters; the log is cut to stay under this

LOG = deque(maxlen=LOG_LINES)    # (appending is safe from every thread)


def log(text):
    now = f"{datetime.now():%H:%M:%S}"
    LOG.extend(f"{now}  {line}" for line in text.rstrip().splitlines())


def read_token():
    """The GitHub token reports are sent with, or None: this copy can't send any."""
    here = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(here, TOKEN_FILE), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


REPORT_TOKEN = read_token()


def anonymise(text, folders=(), stems=()):
    """text without the user's name, folders ("<folder>") and file names ("<file>")."""
    home = os.path.expanduser("~")
    places = {p for p in (*folders, home) if len(p) > 3}     # (not "C:\" or "/")
    for p in sorted(places, key=len, reverse=True):
        for spelling in (p, p.replace("\\", "/")):
            text = re.sub(re.escape(spelling), "<folder>", text, flags=re.I)
    users = {os.environ.get("USERNAME"), os.environ.get("USER"), os.path.basename(home)}
    for words, new in ((stems, "<file>"), (users, "<user>")):
        for w in sorted(filter(None, words), key=len, reverse=True):
            # (whole words only: a file called "a" must not eat every a in the log)
            text = re.sub(rf"(?<![^\W_]){re.escape(w)}(?![^\W_])", new, text, flags=re.I)
    return text


def make_report(category, text, details):
    """The report from the template: what the app knows, then the user's text, then the log."""
    built = "" if getattr(sys, "frozen", False) else " (from source)"
    head = (f"- **Version:** {VERSION}{built}\n"
            f"- **Date:** {datetime.now():%Y-%m-%d %H:%M} ({datetime.now(timezone.utc):%H:%M} UTC)\n"
            f"- **System:** {platform.platform()} · Python {platform.python_version()}\n"
            f"- **FFmpeg:** {'found' if FFMPEG else 'not found'}\n"
            f"- **LibreOffice:** {'found' if SOFFICE else 'not found'}\n"
            f"{details}\n"
            f"## What happened\n\n{text.strip()[:REPORT_TEXT_MAX]}\n\n")
    summary = f"Log (last {LOG_LINES} lines)"
    logged = "\n".join(LOG) or "(nothing logged yet)"
    room = ISSUE_BODY_MAX - len(head) - len(summary) - 60
    if len(logged) > room:
        logged = "…" + logged[-room:]
    # (a longer fence, in case the log holds ``` itself)
    return {"title": f"{category} ({VERSION})",
            "body": f"{head}<details><summary>{summary}</summary>\n\n"
                    f"````\n{logged}\n````\n\n</details>\n"}


def create_issue(report):
    """Opens the issue (label "bug") and returns its page. Raises if it can't."""
    request = urllib.request.Request(
        ISSUES_URL,
        data=json.dumps({**report, "labels": ["bug"]}).encode("utf-8"),
        headers={"Authorization": f"Bearer {REPORT_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "AnyConverter"})
    with urllib.request.urlopen(request, timeout=20) as answer:
        return json.load(answer)["html_url"]


def send_report(report):
    """Sends a report, or keeps it for the next start. Returns whether it went."""
    try:
        log(f"Bug report sent: {create_issue(report)}")
        return True
    except Exception as e:
        log(f"Couldn't send the bug report: {e}")
    try:
        os.makedirs(WAITING_REPORTS, exist_ok=True)
        name = f"{datetime.now():%Y-%m-%d %H-%M} {uuid.uuid4().hex[:6]}.json"
        with open(os.path.join(WAITING_REPORTS, name), "w", encoding="utf-8") as f:
            json.dump(report, f)
    except OSError as e:
        log(f"Couldn't keep the bug report: {e}")
    return False


def send_waiting_reports():
    """At the start: reports that couldn't go last time go now."""
    if not REPORT_TOKEN or not os.path.isdir(WAITING_REPORTS):
        return
    for name in sorted(os.listdir(WAITING_REPORTS)):
        path = os.path.join(WAITING_REPORTS, name)
        try:
            with open(path, encoding="utf-8") as f:
                create_issue(json.load(f))
            os.remove(path)
        except Exception as e:      # (most likely still offline: the rest would fail too)
            log(f"Couldn't send a waiting bug report: {e}")
            return


# --------------------------------------------------------------------- UI ---

BG = "#0f1117"
CARD = "#181b24"
CARD2 = "#20242f"
FG = "#e6e8ee"
MUTED = "#8b91a1"
ACCENT = "#7c5cff"
ACCENT_HOVER = "#957bff"
OK = "#3ecf8e"
ERR = "#ff5c7a"
FONT = "Segoe UI" if IS_WIN else "DejaVu Sans"
MONO = "Consolas" if IS_WIN else "DejaVu Sans Mono"


class Button(tk.Label):
    """Flat, rounded-looking button with hover effect."""

    def __init__(self, master, text, command, primary=False, **kw):
        self.bg = ACCENT if primary else CARD2
        self.hover = ACCENT_HOVER if primary else "#2a2f3c"
        super().__init__(master, text=text, bg=self.bg, fg=FG, cursor="hand2",
                         font=(FONT, 11 if primary else 10, "bold" if primary else "normal"),
                         padx=18, pady=10 if primary else 7, **kw)
        self.command = command
        self.enabled = True
        self.bind("<Enter>", lambda e: self.enabled and self.config(bg=self.hover))
        self.bind("<Leave>", lambda e: self.config(bg=self.bg if self.enabled else "#2a2d36"))
        self.bind("<Button-1>", lambda e: self.enabled and self.command())

    def set_enabled(self, on, text=None):
        self.enabled = on
        self.config(bg=self.bg if on else "#2a2d36", fg=FG if on else MUTED,
                    cursor="hand2" if on else "arrow")
        if text:
            self.config(text=text)


class ToolDialog(tk.Toplevel):
    """Friendly 'FFmpeg / LibreOffice is missing' guide with one-click install
    (winget on Windows, the distro's package manager on Linux)."""

    def __init__(self, master, tool, on_found):
        super().__init__(master)
        self.tool = tool
        self.on_found = on_found
        spec = TOOLS[tool]
        name = spec["name"]
        self.title(f"{name} needed")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.cmd = install_command(tool)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=26, pady=22)

        tk.Label(body, text="One quick step before you start", bg=BG, fg=FG,
                 font=(FONT, 15, "bold")).pack(anchor="w")
        tk.Label(body, bg=BG, fg=MUTED, font=(FONT, 10), justify="left", wraplength=470,
                 text=f"AnyConverter uses a free program called {name} for {spec['purpose']}. "
                      f"It isn't installed on this computer yet. It only takes a few minutes:"
                 ).pack(anchor="w", pady=(6, 16))

        def step(num, title, text):
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", pady=(0, 12))
            tk.Label(row, text=str(num), bg=ACCENT, fg=FG, font=(FONT, 10, "bold"),
                     width=2).pack(side="left", anchor="n", padx=(0, 12), ipady=2)
            col = tk.Frame(row, bg=BG)
            col.pack(side="left", fill="x", expand=True)
            tk.Label(col, text=title, bg=BG, fg=FG, font=(FONT, 10, "bold")).pack(anchor="w")
            tk.Label(col, text=text, bg=BG, fg=MUTED, font=(FONT, 9), justify="left",
                     wraplength=430).pack(anchor="w")
            return col

        ready = f"AnyConverter will find {name} and you're ready to go."
        if self.cmd and IS_WIN:
            col = step(1, f"Install {name}",
                       f"Click “Install for me”. A black window will open and install {name}. "
                       "If it asks you to agree to terms, type Y and press Enter.\n"
                       "Or, if you prefer, copy this command into PowerShell yourself:")
            self._cmd_box(col)
            step(2, "Wait until it says “Successfully installed”",
                 "Then close that black window.")
            step(3, "Click “Check again” below", ready)
        elif self.cmd:
            col = step(1, f"Install {name}",
                       "Click “Install for me”. A terminal window will open. Type your "
                       "password when asked (nothing shows while you type, that's normal) "
                       "and press Enter.\n"
                       "Or, if you prefer, copy this command into a terminal yourself:")
            self._cmd_box(col)
            step(2, "Wait until it finishes", "Then press Enter to close the terminal.")
            step(3, "Click “Check again” below", ready)
        elif not IS_WIN:
            step(1, f"Install the “{name.lower()}” package",
                 "Open your distribution's software center (or package manager), "
                 f"search for “{name.lower()}” and install it.")
            step(2, "Click “Check again” below", ready)
        elif tool == "ffmpeg":
            step(1, "Download FFmpeg",
                 "Go to ffmpeg.org/download.html, download a Windows build and unzip it.")
            step(2, "Copy two files next to AnyConverter",
                 "From its “bin” folder, copy ffmpeg.exe and ffprobe.exe into the same "
                 "folder as AnyConverter.")
            step(3, "Click “Check again” below", ready)
        else:
            step(1, f"Download and install {name}",
                 "Click “Open download page”, download the Windows installer and run it.")
            step(2, "Click “Check again” below", ready)

        self.msg = tk.Label(body, text="", bg=BG, fg=ERR, font=(FONT, 9))
        self.msg.pack(anchor="w", pady=(2, 10))

        btns = tk.Frame(body, bg=BG)
        btns.pack(fill="x")
        Button(btns, "Check again", self.recheck, primary=True).pack(side="right")
        if self.cmd:
            Button(btns, "Install for me", self.install).pack(side="right", padx=8)
        elif IS_WIN:
            Button(btns, "Open download page", lambda: __import__("webbrowser").open(
                TOOLS[tool]["url"])).pack(side="right", padx=8)
        Button(btns, "Later", self.destroy).pack(side="left")

        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()
        self.focus_set()

    def _cmd_box(self, parent):
        cmd_row = tk.Frame(parent, bg=CARD2)
        cmd_row.pack(fill="x", pady=(8, 0))
        tk.Label(cmd_row, text=self.cmd, bg=CARD2, fg="#c9b8ff",
                 font=(MONO, 10), padx=10, pady=8).pack(side="left")
        self.copy_btn = Button(cmd_row, "Copy", self.copy_cmd)
        self.copy_btn.pack(side="right", padx=4, pady=4)

    def copy_cmd(self):
        self.clipboard_clear()
        self.clipboard_append(self.cmd)
        self.copy_btn.config(text="Copied!")
        self.after(1500, lambda: self.copy_btn.config(text="Copy"))

    def install(self):
        try:
            if open_in_terminal(self.cmd):
                self.msg.config(text="Installing in the other window… click “Check again” "
                                     "when it's done.", fg=MUTED)
            else:
                self.msg.config(text="Couldn't find a terminal app. Click “Copy” and paste "
                                     "the command into your terminal.", fg=ERR)
        except Exception as e:
            self.msg.config(text=f"Couldn't start the installer: {e}", fg=ERR)

    def recheck(self):
        if DETECT[self.tool]():
            self.destroy()
            self.on_found()
        else:
            self.msg.config(text="Still not found. If the install just finished, wait a few "
                                 "seconds and try again.", fg=ERR)


class BugDialog(tk.Toplevel):
    """'Report a bug': one text box and Send. The category and what the app knows are
    taken when it opens, so they describe what the user was looking at."""

    def __init__(self, master):
        super().__init__(master)
        self.category = master.report_category()
        self.details = master.report_details()
        self.title("Report a bug")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=26, pady=22)

        tk.Label(body, text="Report a bug", bg=BG, fg=FG,
                 font=(FONT, 15, "bold")).pack(anchor="w")
        tk.Label(body, text=f"Bug in: {self.category}", bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(anchor="w", pady=(2, 12))
        tk.Label(body, text="What happened? What did you do just before, and what did you expect?",
                 bg=BG, fg=FG, font=(FONT, 10)).pack(anchor="w", pady=(0, 6))

        self.box = tk.Text(body, width=58, height=9, wrap="word", bg=CARD2, fg=FG,
                           insertbackground=FG, relief="flat", bd=0, padx=10, pady=8,
                           font=(FONT, 10), highlightthickness=0)
        self.box.pack(fill="x")
        self.box.bind("<<Modified>>", self._changed)

        tk.Label(body, bg=BG, fg=MUTED, font=(FONT, 9), justify="left", wraplength=470,
                 text="AnyConverter adds its version, your system, the formats you're converting "
                      "and the end of its log (your file names and user name are left out). "
                      "It's posted as a public issue on GitHub, so please keep private details "
                      "out of your text."
                 ).pack(anchor="w", pady=(10, 4))
        self.msg = tk.Label(body, text="", bg=BG, fg=ERR, font=(FONT, 9))
        self.msg.pack(anchor="w", pady=(2, 10))

        btns = tk.Frame(body, bg=BG)
        btns.pack(fill="x")
        self.send_btn = Button(btns, "Send", self.send, primary=True)
        self.send_btn.pack(side="right")
        self.send_btn.set_enabled(False)
        Button(btns, "Cancel", self.destroy).pack(side="left")
        if not REPORT_TOKEN:
            self.msg.config(text="This version can't send reports (it has no key for it).")

        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()
        self.box.focus_set()

    def _changed(self, event):
        self.box.edit_modified(False)
        self.send_btn.set_enabled(bool(REPORT_TOKEN and self.box.get("1.0", "end").strip()))

    def send(self):
        self.send_btn.set_enabled(False)
        self.box.config(state="disabled")
        self.msg.config(text="Sending…", fg=MUTED)
        report = make_report(self.category, self.box.get("1.0", "end"), self.details)
        threading.Thread(target=lambda: self.master.msgs.put(
            ("reported", self, send_report(report))), daemon=True).start()

    def finished(self, ok):
        if not self.winfo_exists():
            return
        self.msg.config(text="Thanks! Max will look at it." if ok else
                        "Couldn't send it now: it goes the next time you start AnyConverter.",
                        fg=OK if ok else ERR)
        self.after(1500 if ok else 3000, self.destroy)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AnyConverter")
        self.geometry("820x690")
        self.minsize(700, 560)
        self.configure(bg=BG)
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        self.files = []          # list of paths
        self.msgs = queue.Queue()
        self.worker = None
        self.procs = set()       # running ffmpeg / LibreOffice processes
        self.lock = threading.Lock()
        self.cancelled = False

        self.fmt = tk.StringVar(value="mp4")
        self.quality = tk.StringVar(value="Medium")
        self.outdir = tk.StringVar(value="")  # "" = same folder as source
        self.merge = tk.BooleanVar(value=False)
        # (Quiznight opens AnyConverter with --max-size 1280x720: its videos needn't be bigger)
        self.max_size = None
        # Every folder and file name seen, so the log can leave them out (also after "Clear")
        self.folders = set()
        self.stems = set()
        threading.excepthook = self._thread_error

        self._style()
        self._build()
        self._apply_args(sys.argv[1:])
        self.after(100, self._poll)
        threading.Thread(target=send_waiting_reports, daemon=True).start()

        if not FFMPEG:
            self.status.config(text="FFmpeg is not installed (needed for audio & video). "
                                    "Click Convert for help", fg=ERR)
            self.after(300, lambda: self.show_tool_help("ffmpeg"))

    def _apply_args(self, args):
        """AnyConverter [--format EXT] [--max-size WxH] [FILE ...]: preselect the target format, limit
        videos' size, and add files (Quiznight opens it with --format ogv --max-size 1280x720 and the video)."""
        paths = []
        it = iter(args)
        for a in it:
            if a == "--max-size":
                try:
                    w, h = next(it, "").lower().split("x")
                    self.max_size = (int(w), int(h))
                except ValueError:
                    pass
            elif a == "--format":
                ext = next(it, "").lower().lstrip(".")
                match = [v for v in self.fmt_box["values"] if v.split()[0] == ext]
                if match:
                    self.fmt_box.set(match[0])
            elif os.path.isfile(a):
                paths.append(a)
        self.add_paths(paths)

    def show_tool_help(self, tool):
        ToolDialog(self, tool, on_found=lambda: self.status.config(
            text=f"{TOOLS[tool]['name']} found, you're all set!", fg=OK))

    # ---- bug reports
    def _log(self, text):
        log(self._private(text))

    def _private(self, text):
        return anonymise(text, list(self.folders), list(self.stems))

    def report_callback_exception(self, *exc):
        """An error in a button or timer: built copies have no console to show it on."""
        self._log("".join(traceback.format_exception(*exc)))
        super().report_callback_exception(*exc)

    def _thread_error(self, args):
        self._log("".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback)))
        threading.__excepthook__(args)

    def report_category(self):
        """What the report is about: the formats on screen, "mkv, mp4 → webm"."""
        if not self.files:
            return "General"
        exts = sorted({split_ext(p)[1] or "no extension" for p in self.files})
        shown = ", ".join(exts[:3]) + (" …" if len(exts) > 3 else "")
        return f"{shown} → {self.fmt_box.get().split()[0]}"

    def report_details(self):
        """The report's lines about the app's state (never a file's name)."""
        kinds = Counter(split_ext(p)[1] or "no extension" for p in self.files)
        files = ", ".join(f"{n} {e}" for e, n in kinds.most_common()) or "none"
        size = "x".join(map(str, self.max_size)) if self.max_size else "any"
        return (f"- **Files:** {files}\n"
                f"- **Quality / max size:** {self.quality.get()} / {size}\n"
                f"- **Status:** {self._private(self.status.cget('text'))}\n")

    def report_bug(self):
        BugDialog(self)

    # ---- styling
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TCombobox", fieldbackground=CARD2, background=CARD2, foreground=FG,
                    arrowcolor=FG, bordercolor=CARD2, lightcolor=CARD2, darkcolor=CARD2,
                    padding=6)
        s.map("TCombobox", fieldbackground=[("readonly", CARD2)],
              foreground=[("readonly", FG)], selectbackground=[("readonly", CARD2)],
              selectforeground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", CARD2)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.font", (FONT, 10))
        s.configure("Accent.Horizontal.TProgressbar", troughcolor=CARD2, background=ACCENT,
                    bordercolor=CARD2, lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG,
                    rowheight=30, borderwidth=0, font=(FONT, 10))
        s.configure("Treeview.Heading", background=CARD2, foreground=MUTED, borderwidth=0,
                    font=(FONT, 9, "bold"), padding=6)
        s.map("Treeview", background=[("selected", "#2d2850")])
        s.map("Treeview.Heading", background=[("active", CARD2)])
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        s.configure("Vertical.TScrollbar", background=CARD2, troughcolor=CARD,
                    bordercolor=CARD, arrowcolor=MUTED)

    def _label(self, parent, text, size=10, color=MUTED, bold=False, bg=CARD):
        return tk.Label(parent, text=text, bg=bg, fg=color,
                        font=(FONT, size, "bold" if bold else "normal"))

    # ---- layout
    def _build(self):
        pad = dict(padx=22)

        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", pady=(20, 12), **pad)
        Button(head, "Report a bug", self.report_bug).pack(side="right", pady=(8, 0))
        tk.Label(head, text="AnyConverter", bg=BG, fg=FG,
                 font=(FONT, 22, "bold")).pack(side="left")
        tk.Label(head, text="  audio, video, images, PDFs, documents & more", bg=BG, fg=MUTED,
                 font=(FONT, 11)).pack(side="left", pady=(8, 0))

        # file list card
        card = tk.Frame(self, bg=CARD)
        card.pack(fill="both", expand=True, **pad)

        bar = tk.Frame(card, bg=CARD)
        bar.pack(fill="x", padx=14, pady=(12, 8))
        self._label(bar, "FILES", 9, bold=True).pack(side="left")
        self.count_lbl = self._label(bar, "", 9)
        self.count_lbl.pack(side="left", padx=8)
        Button(bar, "Clear", self.clear_files).pack(side="right")
        Button(bar, "Remove selected", self.remove_selected).pack(side="right", padx=6)
        self.add_btn = Button(bar, "+ Add files", self.add_files)
        self.add_btn.pack(side="right")

        tree_wrap = tk.Frame(card, bg=CARD)
        tree_wrap.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.tree = ttk.Treeview(tree_wrap, columns=("name", "status"), show="headings",
                                 selectmode="extended")
        self.tree.heading("name", text="NAME", anchor="w")
        self.tree.heading("status", text="STATUS", anchor="w")
        self.tree.column("name", anchor="w", stretch=True)
        self.tree.column("status", width=210, anchor="w", stretch=False)
        self.tree.tag_configure("ok", foreground=OK)
        self.tree.tag_configure("err", foreground=ERR)
        self.tree.tag_configure("run", foreground=ACCENT_HOVER)
        self.tree.tag_configure("skip", foreground=MUTED)
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.empty_lbl = tk.Label(self.tree, text="Click  “+ Add files”  to choose files to convert",
                                  bg=CARD, fg=MUTED, font=(FONT, 11), cursor="hand2")
        self.empty_lbl.bind("<Button-1>", lambda e: self.add_files())
        self.empty_lbl.place(relx=0.5, rely=0.5, anchor="center")

        # options card
        opts = tk.Frame(self, bg=CARD)
        opts.pack(fill="x", pady=12, **pad)
        inner = tk.Frame(opts, bg=CARD)
        inner.pack(fill="x", padx=14, pady=12)
        inner.columnconfigure(2, weight=1)

        self._label(inner, "CONVERT TO", 9, bold=True).grid(row=0, column=0, sticky="w")
        self._label(inner, "QUALITY", 9, bold=True).grid(row=0, column=1, sticky="w", padx=(16, 0))
        self._label(inner, "SAVE TO", 9, bold=True).grid(row=0, column=2, sticky="w", padx=(16, 0))

        self.fmt_box = ttk.Combobox(inner, state="readonly", width=20, font=(FONT, 10),
                                    height=20)
        self.fmt_box.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.fmt_box.bind("<<ComboboxSelected>>", lambda e: self._refresh_merge())
        self.merge_chk = tk.Checkbutton(
            inner, text="Combine all files into one PDF", variable=self.merge,
            bg=CARD, fg=FG, selectcolor=CARD2, activebackground=CARD, activeforeground=FG,
            font=(FONT, 10), bd=0, highlightthickness=0, cursor="hand2")
        self.merge_chk.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        q = ttk.Combobox(inner, textvariable=self.quality, values=list(QUALITY),
                         state="readonly", width=10, font=(FONT, 10))
        q.grid(row=1, column=1, sticky="w", padx=(16, 0), pady=(6, 0))

        out = tk.Frame(inner, bg=CARD)
        out.grid(row=1, column=2, sticky="ew", padx=(16, 0), pady=(6, 0))
        self.out_lbl = tk.Label(out, text="Same folder as each file", bg=CARD2, fg=FG,
                                font=(FONT, 10), anchor="w", padx=10, pady=6)
        self.out_lbl.pack(side="left", fill="x", expand=True)
        Button(out, "Browse", self.pick_outdir).pack(side="left", padx=(6, 0))
        Button(out, "Reset", self.reset_outdir).pack(side="left", padx=(6, 0))

        # bottom bar
        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill="x", pady=(0, 20), **pad)
        left = tk.Frame(bottom, bg=BG)
        left.pack(side="left", fill="x", expand=True)
        self.status = tk.Label(left, text="Ready", bg=BG, fg=MUTED, font=(FONT, 10), anchor="w")
        self.status.pack(fill="x")
        self.progress = ttk.Progressbar(left, style="Accent.Horizontal.TProgressbar",
                                        maximum=100)
        self.progress.pack(fill="x", pady=(6, 0))
        self.go_btn = Button(bottom, "Convert", self.start, primary=True)
        self.go_btn.pack(side="right", padx=(16, 0))

        self._refresh_count()

    # ---- file management
    def add_files(self):
        if self.worker:
            return
        paths = filedialog.askopenfilenames(title="Choose files to convert",
                                            filetypes=FILE_TYPES)
        self.add_paths(paths)

    def add_paths(self, paths):
        for p in paths:
            p = os.path.normpath(p)
            if p not in self.files:
                self.files.append(p)
                self.folders.add(os.path.dirname(p))
                self.stems.add(split_ext(p)[0])
                self.tree.insert("", "end", iid=p, values=(os.path.basename(p), "Waiting"))
        self._refresh_count()

    def remove_selected(self):
        if self.worker:
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.files.remove(iid)
        self._refresh_count()

    def clear_files(self):
        if self.worker:
            return
        self.tree.delete(*self.tree.get_children())
        self.files.clear()
        self.progress["value"] = 0
        self.status.config(text="Ready", fg=MUTED)
        self._refresh_count()

    def _refresh_count(self):
        n = len(self.files)
        self.count_lbl.config(text=f"{n} file{'s' if n != 1 else ''}" if n else "")
        if n:
            self.empty_lbl.place_forget()
        else:
            self.empty_lbl.place(relx=0.5, rely=0.5, anchor="center")
        self._refresh_formats()

    def _refresh_formats(self):
        """Offer every format at least one of the files can become (all when empty)."""
        wanted = set()
        for p in self.files:
            wanted.update(outputs_for(p))
        seen, values = set(), []
        for label, fmts in FORMAT_GROUPS:
            for f in fmts:
                if f not in seen and (not self.files or f in wanted):
                    seen.add(f)
                    values.append(f"{f}   ·  {label}")
        current = self.fmt_box.get()
        self.fmt_box["values"] = values
        if current not in values:
            self.fmt_box.set(values[0] if values else "")
        self._refresh_merge()

    def _refresh_merge(self):
        if self.fmt_box.get().split()[:1] == ["pdf"] and len(self.files) > 1:
            self.merge_chk.grid()
        else:
            self.merge_chk.grid_remove()

    def pick_outdir(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.outdir.set(os.path.normpath(d))
            self.folders.add(self.outdir.get())
            self.out_lbl.config(text=self.outdir.get())

    def reset_outdir(self):
        self.outdir.set("")
        self.out_lbl.config(text="Same folder as each file")

    # ---- conversion
    def start(self):
        if self.worker:   # acts as Cancel while running
            self.cancelled = True
            self._kill_all()
            return
        if not self.files:
            messagebox.showinfo("No files", "Add some files first.")
            return
        ext = self.fmt_box.get().split()[0]
        jobs = list(self.files)
        merge = ext == "pdf" and self.merge.get() and len(jobs) > 1
        engines = {"pdf"} if merge else {engine_for(p, ext) for p in jobs} - {None}
        for tool, found in (("ffmpeg", FFMPEG), ("office", SOFFICE)):
            if tool in engines and not found and not DETECT[tool]():
                self.show_tool_help(tool)
                return
        need = {}
        for e in engines:
            need.update(PACKAGES.get(e, {}))
        if any(split_ext(p)[1] in ("heic", "heif") for p in jobs):
            need["pillow_heif"] = "pillow-heif"
        if "data" in engines and (ext == "xlsx" or any(split_ext(p)[1] == "xlsx" for p in jobs)):
            need["openpyxl"] = "openpyxl"
        missing = [pip for mod, pip in need.items() if importlib.util.find_spec(mod) is None]
        if missing:
            messagebox.showerror(
                "Missing Python packages",
                "This conversion needs a few extra Python packages. Install them with:\n\n"
                f"    pip install {' '.join(missing)}\n\n"
                "(The ready-made AnyConverter app already includes them.)")
            return
        for p in jobs:
            self.tree.item(p, values=(os.path.basename(p), "Waiting"), tags=())
        self._log(f"Converting {len(jobs)} file(s) to {ext}, {self.quality.get()}"
                  + (", combined" if merge else ""))
        self.cancelled = False
        self.go_btn.config(text="Cancel")
        self.add_btn.set_enabled(False)
        self.worker = threading.Thread(
            target=self._run,
            args=(jobs, ext, self.quality.get(), self.outdir.get(), merge),
            daemon=True)
        self.worker.start()

    def _kill_all(self):
        with self.lock:
            procs = list(self.procs)
        for p in procs:
            if p.poll() is None:
                kill_tree(p)

    def _run(self, jobs, ext, quality, outdir, merge=False):
        self.job_pct = dict.fromkeys(jobs, 0.0)
        self.reserved = set()
        if merge:
            ok = self._merge_pdf(jobs, quality, outdir)
            if not ok:
                self._log(f"Combining failed: {self._last_error[1]}")
            self.msgs.put(("finished", int(ok), int(not ok), self.cancelled, 0))
            return
        # Video encoders already use every core, so videos go one at a time.
        # Audio encoders and the other converters use one core each, so
        # several files run at once.
        video = (ext in VIDEO_FORMATS or ext == "gif") and \
            any(engine_for(p, ext) == "ffmpeg" for p in jobs)
        workers = 1 if video else max(1, min(len(jobs), (os.cpu_count() or 2) // 2, 8))
        if workers > 1:
            self.msgs.put(("status", f"Converting {len(jobs)} files to {ext.upper()} "
                                     f"({workers} at a time)…"))
        with ThreadPoolExecutor(workers) as pool:
            results = list(pool.map(
                lambda ij: self._convert_one(*ij, len(jobs), ext, quality, outdir, workers == 1),
                enumerate(jobs)))
        self.msgs.put(("finished", results.count("ok"), results.count("fail"), self.cancelled,
                       results.count("skip")))

    def _set_pct(self, src, pct):
        self.job_pct[src] = pct
        self.msgs.put(("progress", sum(self.job_pct.values()) / len(self.job_pct)))
        self.msgs.put(("row", src, f"Converting… {pct:4.0f}%", "run"))

    def _target_path(self, src, ext, outdir):
        name = split_ext(src)[0]
        folder = outdir or os.path.dirname(src)
        dst = os.path.join(folder, f"{name}.{ext}")
        if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(src)):
            dst = os.path.join(folder, f"{name} (converted).{ext}")
        return self._unique(dst, ext)

    def _unique(self, path, ext=""):
        """path, or "name (1).ext" etc. if that is taken (ext="" for folders)."""
        with self.lock:   # parallel jobs must not pick the same output name
            root = path[:-len(ext) - 1] if ext else path
            tail = f".{ext}" if ext else ""
            i = 0
            while os.path.exists(path) or path in self.reserved:
                i += 1
                path = f"{root} ({i}){tail}"
            self.reserved.add(path)
        return path

    def _convert_one(self, i, src, total, ext, quality, outdir, show_status):
        if self.cancelled:
            self.msgs.put(("row", src, "Cancelled", "err"))
            return "cancel"
        engine = engine_for(src, ext)
        if engine is None or SAME.get(split_ext(src)[1], split_ext(src)[1]) == ext:
            why = "Already " + ext.upper() if engine else "Can't make " + ext.upper()
            self.msgs.put(("row", src, f"Skipped · {why}", "skip"))
            self.job_pct[src] = 100.0
            return "skip"
        dst = self._target_path(src, ext, outdir)
        self.msgs.put(("row", src, "Starting…", "run"))
        if show_status:
            self.msgs.put(("status", f"[{i + 1}/{total}]  {os.path.basename(src)}  →  {ext.upper()}"))
        if engine == "ffmpeg":
            code, err = self._convert_media(src, dst, ext, quality)
        else:
            try:
                getattr(self, "_conv_" + engine)(src, dst, ext, quality)
                code, err = (1, "Cancelled") if self.cancelled else (0, "")
            except Exception as e:
                code, err = 1, str(e) or type(e).__name__

        if self.cancelled:
            self._remove(dst)
            self.msgs.put(("row", src, "Cancelled", "err"))
            result = "cancel"
        elif code == 0:
            self.msgs.put(("row", src, "✓ Done", "ok"))
            result = "ok"
        else:
            self._remove(dst)
            self.msgs.put(("row", src, "✗ Failed", "err"))
            self.msgs.put(("log", os.path.basename(src), err))
            result = "fail"
        self._log(f"{src} to {ext} ({engine}): {result}" + (f"\n{err}" if result == "fail" else ""))
        self.job_pct[src] = 100.0
        self.msgs.put(("progress", sum(self.job_pct.values()) / len(self.job_pct)))
        return result

    def _convert_media(self, src, dst, ext, quality):
        if KIND_OF.get(split_ext(src)[1]) == "subtitles":
            return self._ffmpeg_one(src, dst, [], None)
        info = probe_info(src)
        # Fastest first; if FFmpeg rejects an attempt (a stream that can't be
        # copied after all), fall back to re-encoding audio, then everything.
        attempts = []
        for copy in ("va", "v", ""):
            a = build_args(ext, quality, info, copy, self.max_size)
            if a not in attempts:
                attempts.append(a)

        for args in attempts:
            if "-c:v" in args and args[args.index("-c:v") + 1] in SPLIT_ENCODERS \
                    and (info["duration"] or 0) >= 6 and info["fps"]:
                code, err = self._parallel_video(src, dst, args, info["duration"], info["rate"],
                                                 info["fps"], info["vstart"], info["astart"])
            else:
                code, err = self._ffmpeg_one(src, dst, args, info["duration"])
            if code == 0 or self.cancelled:
                break
        return code, err

    # ---- images (Pillow)
    def _conv_image(self, src, dst, ext, quality):
        from PIL import Image, ImageOps
        _heif()
        self._set_pct(src, 10)
        with Image.open(src) as im:
            if getattr(im, "n_frames", 1) > 1 and ext in ("gif", "webp", "tiff"):
                # animation stays an animation (Pillow converts every frame)
                kw = {"save_all": True, "loop": im.info.get("loop", 0)}
                if ext == "webp":
                    kw["quality"] = IMAGE_QUALITY[quality]
                elif ext == "tiff":
                    kw = {"save_all": True, "compression": "tiff_lzw"}
                im.save(dst, PIL_FORMAT[ext], **kw)
                return
            img = ImageOps.exif_transpose(im)    # phone photos: apply the rotation
            save_image(img, dst, ext, quality)

    # ---- PDF (pdfium)
    def _conv_pdf(self, src, dst, ext, quality):
        import pypdfium2 as pdfium
        with PDFIUM_LOCK:     # pdfium isn't thread-safe
            pdf = pdfium.PdfDocument(src)
            try:
                n = len(pdf)
                if ext == "txt":
                    parts = []
                    for k in range(n):
                        page = pdf[k]
                        text = page.get_textpage()
                        parts.append(text.get_text_bounded())
                        text.close()
                        page.close()
                        self._set_pct(src, (k + 1) / n * 95)
                    with open(dst, "w", encoding="utf-8", newline="\n") as f:
                        f.write("\n\n".join(p.replace("\r\n", "\n").strip() for p in parts) + "\n")
                    return
                # one image per page; several pages go into their own folder
                if n > 1:
                    folder = self._unique(dst[:-len(ext) - 1])
                    os.makedirs(folder)
                    outs = [os.path.join(folder, f"page {k + 1:0{len(str(n))}d}.{ext}")
                            for k in range(n)]
                else:
                    folder, outs = None, [dst]
                try:
                    for k in range(n):
                        if self.cancelled:
                            raise RuntimeError("Cancelled")
                        page = pdf[k]
                        img = page.render(scale=PDF_DPI[quality] / 72).to_pil()
                        page.close()
                        save_image(img, outs[k], ext, quality)
                        self._set_pct(src, (k + 1) / n * 100)
                except BaseException:
                    if folder:
                        shutil.rmtree(folder, ignore_errors=True)
                    raise
            finally:
                pdf.close()

    def _merge_pdf(self, jobs, quality, outdir):
        """All PDFs and images, in list order, into one "Combined.pdf"."""
        import pypdfium2 as pdfium
        from PIL import Image, ImageOps
        _heif()
        usable = [p for p in jobs if KIND_OF.get(split_ext(p)[1]) in ("pdf", "image")]
        for p in jobs:
            if p not in usable:
                self.msgs.put(("row", p, "Skipped · not a PDF or image", "skip"))
        if not usable:
            self._last_error = ("", "None of the files is a PDF or an image.")
            return False
        dst = self._unique(os.path.join(outdir or os.path.dirname(usable[0]), "Combined.pdf"),
                           "pdf")
        self.msgs.put(("status", f"Combining {len(usable)} files into {os.path.basename(dst)}…"))
        try:
            with PDFIUM_LOCK:
                out = pdfium.PdfDocument.new()
                opened = []
                for k, p in enumerate(usable):
                    if self.cancelled:
                        raise RuntimeError("Cancelled")
                    self.msgs.put(("row", p, "Adding…", "run"))
                    if split_ext(p)[1] == "pdf":
                        part = pdfium.PdfDocument(p)
                    else:
                        buf = io.BytesIO()
                        with Image.open(p) as im:
                            save_image(ImageOps.exif_transpose(im), buf, "pdf", quality)
                        part = pdfium.PdfDocument(buf.getvalue())
                    opened.append(part)
                    out.import_pages(part)
                    self.msgs.put(("row", p, "✓ Added", "ok"))
                    self.msgs.put(("progress", (k + 1) / len(usable) * 95))
                out.save(dst)
                for part in opened:
                    part.close()
                out.close()
        except Exception as e:
            self._remove(dst)
            self._last_error = (os.path.basename(dst), str(e))
            for p in usable:
                self.msgs.put(("row", p, "Cancelled" if self.cancelled else "✗ Failed", "err"))
            return False
        self.msgs.put(("progress", 100))
        return True

    # ---- spreadsheets & data (csv / tsv / json / xlsx)
    def _conv_data(self, src, dst, ext, quality):
        self._set_pct(src, 10)
        rows = read_table(src)
        self._set_pct(src, 50)
        write_table(rows, dst, ext)

    # ---- documents, slides, spreadsheets (LibreOffice)
    def _conv_office(self, src, dst, ext, quality):
        self._set_pct(src, 5)
        with OFFICE_LOCK:     # one LibreOffice at a time: they share a profile
            tmp = tempfile.mkdtemp(prefix="anyconv-")
            try:
                doc = src
                if ext == "txt" and split_ext(src)[1] not in ("odt", "txt"):
                    # LibreOffice's text export can hang on some Word files
                    # (tables); going through its own format first avoids that.
                    doc = self._soffice(src, "odt", os.path.join(tmp, "step"))
                    self._set_pct(src, 50)
                made = self._soffice(doc, ext, tmp)
                if not self.cancelled:
                    shutil.move(made, dst)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def _soffice(self, src, ext, outdir):
        """Convert src into outdir with LibreOffice; returns the new file's path."""
        cmd = [SOFFICE, "-env:UserInstallation=" + _path_uri(OFFICE_PROFILE),
               "--headless", "--norestore", "--nolockcheck"]
        if split_ext(src)[1] in ("html", "htm"):
            cmd.append("--infilter=HTML (StarWriter)")   # as a document, not a web page
        cmd += ["--convert-to", OFFICE_FILTERS.get(ext, ext), "--outdir", outdir, src]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=NO_WINDOW, start_new_session=not IS_WIN)
        with self.lock:
            self.procs.add(proc)
        try:
            out, _ = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            raise RuntimeError("LibreOffice took longer than 5 minutes and was stopped.")
        finally:
            with self.lock:
                self.procs.discard(proc)
        if self.cancelled:
            raise RuntimeError("Cancelled")
        made = os.path.join(outdir, f"{split_ext(src)[0]}.{ext}")
        if not os.path.isfile(made):
            raise RuntimeError("LibreOffice couldn't convert this file.\n" + out[-500:])
        return made

    # ---- archives (zip / tar)
    def _conv_archive(self, src, dst, ext, quality):
        tmp = tempfile.mkdtemp(prefix="anyconv-")
        try:
            self._set_pct(src, 5)
            if zipfile.is_zipfile(src):
                with zipfile.ZipFile(src) as z:
                    z.extractall(tmp)
            else:
                with tarfile.open(src) as t:
                    t.extractall(tmp, filter="data")
            self._set_pct(src, 50)
            if self.cancelled:
                return
            entries = sorted(os.listdir(tmp))
            if ext == "zip":
                with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
                    for root, dirs, files in os.walk(tmp):
                        for name in sorted(dirs + files):
                            full = os.path.join(root, name)
                            z.write(full, os.path.relpath(full, tmp))
            else:
                mode = {"tar.gz": "w:gz", "tar.xz": "w:xz", "tar": "w"}[ext]
                with tarfile.open(dst, mode) as t:
                    for name in entries:
                        t.add(os.path.join(tmp, name), name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _ffmpeg_one(self, src, dst, args, duration, pre_input=(), on_time=None):
        """Run one ffmpeg; report progress for src unless on_time is given.
        Returns (exit code, last error lines)."""
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", *pre_input, "-i", src, *args,
               "-progress", "pipe:1", "-nostats", dst]
        err_tail = []
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        except Exception as e:
            return -1, str(e)
        with self.lock:
            self.procs.add(proc)
        if self.cancelled:
            proc.terminate()
        try:
            for line in proc.stdout:
                line = line.strip()
                # Without ffprobe, take the length from ffmpeg's own "Duration:" line.
                dm = None if duration else re.match(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", line)
                if dm:
                    duration = int(dm.group(1)) * 3600 + int(dm.group(2)) * 60 + float(dm.group(3)) or None
                    continue
                m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
                if m:
                    secs = int(m.group(1)) / 1e6
                    if on_time:
                        on_time(secs)
                    elif duration:
                        self._set_pct(src, min(100.0, secs / duration * 100))
                elif "=" not in line and line:
                    err_tail = (err_tail + [line])[-6:]
            code = proc.wait()
        finally:
            with self.lock:
                self.procs.discard(proc)
        return code, "\n".join(err_tail)

    def _parallel_video(self, src, dst, args, duration, rate, fps, vstart=0.0, astart=0.0):
        """libtheora uses one CPU core and libvpx-vp9 only a few. Encode the
        video as N time slices at the same time, the audio alongside, then
        join them losslessly. Several times faster on multi-core CPUs."""
        enc = args[args.index("-c:v") + 1]
        per_slice = SPLIT_ENCODERS[enc]          # threads each slice uses well
        n = max(2, min((os.cpu_count() or 2) // per_slice, 16, int(duration // 3)))
        aidx = args.index("-c:a") if "-c:a" in args else None
        vend = aidx if aidx is not None else args.index("-an") if "-an" in args else len(args)
        vargs = args[args.index("-c:v"):vend]
        if "-threads" in vargs:
            vargs[vargs.index("-threads") + 1] = str(per_slice)
        ext = os.path.splitext(dst)[1]
        has_audio = aidx is not None
        done = [0.0] * (n + 1)

        def tick(k):
            def f(secs):
                done[k] = secs
                vid = sum(done[:n]) / duration
                aud = (done[n] / duration) if has_audio else 1.0
                self._set_pct(src, min(100.0, (vid * 0.9 + aud * 0.1) * 100))
            return f

        tmp = tempfile.mkdtemp(prefix="anyconv-")
        try:
            jobs = []
            # Every slice is laid on one shared constant-rate frame grid and cut
            # on exact frame numbers, so the joined video has precisely the
            # frames a single-pass encode would, in sync with the audio (also
            # for game clips with uneven frame timing).
            total = round(duration * fps)
            bounds = [round(total * k / n) for k in range(n)]
            for k in range(n):
                part = os.path.join(tmp, f"part{k:02d}{ext}")
                # The seek keeps frames at or after this time (measured from the
                # file's start, hence the video's own offset). Half a frame early
                # puts grid frame bounds[k] at 0.5/fps after the seek point.
                pre = ["-ss", f"{vstart + (bounds[k] - 0.5) / fps:.6f}"] if k else []
                grid_start = 0.5 / fps if k else vstart
                cut = [] if k == n - 1 else ["-frames:v", str(bounds[k + 1] - bounds[k])]
                vf = f"fps={rate}:start_time={grid_start:.6f},setpts=PTS-STARTPTS"
                if "-vf" in args:
                    vf += "," + args[args.index("-vf") + 1]
                jobs.append((part, ["-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf,
                                    *cut, *vargs], pre, tick(k)))
            if has_audio:
                jobs.append((os.path.join(tmp, "audio.mka"),
                             ["-map", "0:a:0", "-vn", *args[aidx:]],   # audio args come last
                             [], tick(n)))
            with ThreadPoolExecutor(len(jobs)) as pool:
                results = list(pool.map(
                    lambda j: self._ffmpeg_one(src, j[0], j[1], duration, j[2], j[3]), jobs))
            for code, err in results:
                if code != 0:
                    return code, err
            listfile = os.path.join(tmp, "parts.txt")
            with open(listfile, "w", encoding="utf-8") as f:
                for k in range(n):
                    f.write(f"file '{os.path.join(tmp, f'part{k:02d}{ext}')}'\n".replace("\\", "/"))
            join = [FFMPEG, "-hide_banner", "-nostdin", "-y",
                    *(["-itsoffset", f"{vstart:.6f}"] if vstart else []),
                    "-f", "concat", "-safe", "0", "-i", listfile]
            if has_audio:
                join += [*(["-itsoffset", f"{astart:.6f}"] if astart else []),
                         "-i", os.path.join(tmp, "audio.mka"), "-map", "0:v", "-map", "1:a"]
            r = subprocess.run([*join, "-c", "copy", dst], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            return r.returncode, r.stderr[-600:]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _remove(path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def _poll(self):
        try:
            while True:
                msg = self.msgs.get_nowait()
                kind = msg[0]
                if kind == "row":
                    _, iid, text, tag = msg
                    if self.tree.exists(iid):
                        self.tree.item(iid, values=(os.path.basename(iid), text), tags=(tag,))
                elif kind == "status":
                    self.status.config(text=msg[1], fg=FG)
                elif kind == "progress":
                    self.progress["value"] = msg[1]
                elif kind == "log":
                    print(f"--- {msg[1]} failed ---\n{msg[2]}\n", file=sys.stderr)
                    self._last_error = (msg[1], msg[2])
                elif kind == "reported":
                    msg[1].finished(msg[2])
                elif kind == "finished":
                    _, done, failed, cancelled, skipped = msg
                    self.worker = None
                    self.go_btn.config(text="Convert")
                    self.add_btn.set_enabled(True)
                    if done == 0 and skipped and not failed and not cancelled:
                        self.status.config(text=f"Nothing to do: {skipped} file(s) can't be "
                                                f"converted to this format", fg=ERR)
                    elif cancelled:
                        self.status.config(text=f"Cancelled  ·  {done} converted", fg=ERR)
                    elif failed:
                        self.status.config(text=f"{done} converted, {failed} failed", fg=ERR)
                        name, log = getattr(self, "_last_error", ("", ""))
                        messagebox.showwarning(
                            "Some files failed",
                            f"{failed} file(s) could not be converted.\n\nLast error ({name}):\n{log[-600:]}")
                    else:
                        extra = f", {skipped} skipped" if skipped else ""
                        self.status.config(text=f"All done  ·  {done} file(s) converted{extra}",
                                           fg=OK)
        except queue.Empty:
            pass
        self.after(80, self._poll)


if __name__ == "__main__":
    App().mainloop()
