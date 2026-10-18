"""AnyConverter - convert any audio/video file into the format you want.

Pure Python + tkinter (no pip installs needed). Uses FFmpeg under the hood,
so FFmpeg must be installed and on your PATH (or placed next to this script).

Run:  py anyconverter.py         (Windows)
      python3 anyconverter.py    (Linux; needs the python3-tk package)
"""

import json
import os
import queue
import tempfile
from concurrent.futures import ThreadPoolExecutor
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


def build_args(ext, q, info, copy="va"):
    """FFmpeg output args for one file. info = probe_info() result.
    copy: which streams may be copied as-is ("va", "v" or "")."""
    venc, aenc = TARGETS[ext]
    vcopy = "v" in copy and q != "Small"
    acopy = "a" in copy and q != "Small"
    args = ["-sn", "-dn"]
    if venc is None:
        args += ["-vn"]
    elif vcopy and info.get("vcodec") in COPY_VIDEO.get(ext, ()):
        args += ["-c:v", "copy"]
        if info["vcodec"] == "hevc" and ext in ("mp4", "m4v", "mov"):
            args += ["-tag:v", "hvc1"]       # so Apple players accept it
    else:
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

VIDEO_FORMATS = ["mp4", "mkv", "mov", "webm", "ogv", "avi", "m4v", "flv", "wmv", "gif"]
AUDIO_FORMATS = ["mp3", "wav", "flac", "m4a", "aac", "ogg", "opus", "wma", "aiff"]

MEDIA_EXTS = (
    "*.mp4 *.mkv *.mov *.webm *.avi *.m4v *.flv *.wmv *.mpg *.mpeg *.3gp *.ts *.mts "
    "*.m2ts *.vob *.ogv *.gif *.mp3 *.wav *.flac *.m4a *.aac *.ogg *.oga *.opus *.wma "
    "*.aiff *.aif *.alac *.amr *.ac3 *.dts *.mka *.caf"
)
if os.name != "nt":   # Linux file names are case-sensitive: also match VIDEO.MP4
    MEDIA_EXTS += " " + MEDIA_EXTS.upper()

# ----------------------------------------------------------------- ffmpeg ---


WINGET_CMD = "winget install -e --id Gyan.FFmpeg"

# Linux package managers -> command that installs FFmpeg
LINUX_INSTALL = [
    ("apt-get", "sudo apt install -y ffmpeg"),
    ("dnf", "sudo dnf install -y ffmpeg-free"),
    ("pacman", "sudo pacman -S --needed ffmpeg"),
    ("zypper", "sudo zypper install -y ffmpeg"),
    ("xbps-install", "sudo xbps-install -S ffmpeg"),
    ("apk", "sudo apk add ffmpeg"),
    ("eopkg", "sudo eopkg install ffmpeg"),
]

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


def install_command():
    """The one-line command that installs FFmpeg on this system, or None."""
    if IS_WIN:
        return WINGET_CMD if shutil.which("winget", path=_fresh_path()) else None
    for tool, cmd in LINUX_INSTALL:
        if shutil.which(tool):
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


FFMPEG = FFPROBE = None
detect_ffmpeg()
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


class FFmpegDialog(tk.Toplevel):
    """Friendly 'FFmpeg is missing' guide with one-click install
    (winget on Windows, the distro's package manager on Linux)."""

    def __init__(self, master, on_found):
        super().__init__(master)
        self.on_found = on_found
        self.title("FFmpeg needed")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.cmd = install_command()

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=26, pady=22)

        tk.Label(body, text="One quick step before you start", bg=BG, fg=FG,
                 font=(FONT, 15, "bold")).pack(anchor="w")
        tk.Label(body, bg=BG, fg=MUTED, font=(FONT, 10), justify="left", wraplength=470,
                 text="AnyConverter uses a free program called FFmpeg to do the actual "
                      "converting. It isn't installed on this computer yet. It only takes a minute:"
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

        if self.cmd and IS_WIN:
            col = step(1, "Install FFmpeg",
                       "Click “Install for me”. A black window will open and install FFmpeg. "
                       "If it asks you to agree to terms, type Y and press Enter.\n"
                       "Or, if you prefer, copy this command into PowerShell yourself:")
            self._cmd_box(col)
            step(2, "Wait until it says “Successfully installed”",
                 "Then close that black window.")
            step(3, "Click “Check again” below", "AnyConverter will find FFmpeg and you're ready to go.")
        elif self.cmd:
            col = step(1, "Install FFmpeg",
                       "Click “Install for me”. A terminal window will open. Type your "
                       "password when asked (nothing shows while you type, that's normal) "
                       "and press Enter.\n"
                       "Or, if you prefer, copy this command into a terminal yourself:")
            self._cmd_box(col)
            step(2, "Wait until it finishes", "Then press Enter to close the terminal.")
            step(3, "Click “Check again” below", "AnyConverter will find FFmpeg and you're ready to go.")
        elif not IS_WIN:
            step(1, "Install the “ffmpeg” package",
                 "Open your distribution's software center (or package manager), "
                 "search for “ffmpeg” and install it.")
            step(2, "Click “Check again” below", "AnyConverter will find FFmpeg and you're ready to go.")
        else:
            step(1, "Download FFmpeg",
                 "Go to ffmpeg.org/download.html, download a Windows build and unzip it.")
            step(2, "Copy two files next to AnyConverter",
                 "From its “bin” folder, copy ffmpeg.exe and ffprobe.exe into the same "
                 "folder as AnyConverter.")
            step(3, "Click “Check again” below", "AnyConverter will find FFmpeg and you're ready to go.")

        self.msg = tk.Label(body, text="", bg=BG, fg=ERR, font=(FONT, 9))
        self.msg.pack(anchor="w", pady=(2, 10))

        btns = tk.Frame(body, bg=BG)
        btns.pack(fill="x")
        Button(btns, "Check again", self.recheck, primary=True).pack(side="right")
        if self.cmd:
            Button(btns, "Install for me", self.install).pack(side="right", padx=8)
        elif IS_WIN:
            Button(btns, "Open download page", lambda: __import__("webbrowser").open(
                "https://ffmpeg.org/download.html")).pack(side="right", padx=8)
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
        if detect_ffmpeg():
            self.destroy()
            self.on_found()
        else:
            self.msg.config(text="Still not found. If the install just finished, wait a few "
                                 "seconds and try again.", fg=ERR)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AnyConverter")
        self.geometry("820x640")
        self.minsize(700, 520)
        self.configure(bg=BG)
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        self.files = []          # list of paths
        self.msgs = queue.Queue()
        self.worker = None
        self.procs = set()       # running ffmpeg processes (several in parallel modes)
        self.lock = threading.Lock()
        self.cancelled = False

        self.fmt = tk.StringVar(value="mp4")
        self.quality = tk.StringVar(value="Medium")
        self.outdir = tk.StringVar(value="")  # "" = same folder as source

        self._style()
        self._build()
        self._apply_args(sys.argv[1:])
        self.after(100, self._poll)

        if not FFMPEG:
            self.status.config(text="FFmpeg is not installed. Click Convert for help", fg=ERR)
            self.after(300, self.show_ffmpeg_help)

    def _apply_args(self, args):
        """AnyConverter [--format EXT] [FILE ...]: preselect the target format and add files
        (Quiznight opens it this way with --format ogv and the video to convert)."""
        paths = []
        it = iter(args)
        for a in it:
            if a == "--format":
                ext = next(it, "").lower().lstrip(".")
                match = [v for v in self.fmt_box["values"] if v.split()[0] == ext]
                if match:
                    self.fmt_box.set(match[0])
            elif os.path.isfile(a):
                paths.append(a)
        self.add_paths(paths)

    def show_ffmpeg_help(self):
        FFmpegDialog(self, on_found=lambda: self.status.config(
            text="FFmpeg found, you're all set!", fg=OK))

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
        tk.Label(head, text="AnyConverter", bg=BG, fg=FG,
                 font=(FONT, 22, "bold")).pack(side="left")
        tk.Label(head, text="  audio & video, any format", bg=BG, fg=MUTED,
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
        self.tree.column("status", width=170, anchor="w", stretch=False)
        self.tree.tag_configure("ok", foreground=OK)
        self.tree.tag_configure("err", foreground=ERR)
        self.tree.tag_configure("run", foreground=ACCENT_HOVER)
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.empty_lbl = tk.Label(self.tree, text="Click  “+ Add files”  to choose audio or video files",
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

        values = ([f"{f}   ·  video" for f in VIDEO_FORMATS]
                  + [f"{f}   ·  audio" for f in AUDIO_FORMATS])
        self.fmt_box = ttk.Combobox(inner, values=values, state="readonly", width=16,
                                    font=(FONT, 10))
        self.fmt_box.set(values[0])
        self.fmt_box.grid(row=1, column=0, sticky="w", pady=(6, 0))

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
        paths = filedialog.askopenfilenames(
            title="Choose audio / video files",
            filetypes=[("Audio & video", MEDIA_EXTS), ("All files", "*.*")])
        self.add_paths(paths)

    def add_paths(self, paths):
        for p in paths:
            p = os.path.normpath(p)
            if p not in self.files:
                self.files.append(p)
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

    def pick_outdir(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.outdir.set(os.path.normpath(d))
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
        if not FFMPEG and not detect_ffmpeg():
            self.show_ffmpeg_help()
            return
        if not self.files:
            messagebox.showinfo("No files", "Add some files first.")
            return
        ext = self.fmt_box.get().split()[0]
        jobs = list(self.files)
        for p in jobs:
            self.tree.item(p, values=(os.path.basename(p), "Waiting"), tags=())
        self.cancelled = False
        self.go_btn.config(text="Cancel")
        self.add_btn.set_enabled(False)
        self.worker = threading.Thread(
            target=self._run,
            args=(jobs, ext, self.quality.get(), self.outdir.get()),
            daemon=True)
        self.worker.start()

    def _kill_all(self):
        with self.lock:
            procs = list(self.procs)
        for p in procs:
            if p.poll() is None:
                p.terminate()

    def _run(self, jobs, ext, quality, outdir):
        self.job_pct = dict.fromkeys(jobs, 0.0)
        self.reserved = set()
        # Video encoders already use every core, so videos go one at a time.
        # Audio encoders use one core each, so several audio files run at once.
        workers = 1 if ext in VIDEO_FORMATS else max(1, min(len(jobs), (os.cpu_count() or 2) // 2, 8))
        if workers > 1:
            self.msgs.put(("status", f"Converting {len(jobs)} files to {ext.upper()} "
                                     f"({workers} at a time)…"))
        with ThreadPoolExecutor(workers) as pool:
            results = list(pool.map(
                lambda ij: self._convert_one(*ij, len(jobs), ext, quality, outdir, workers == 1),
                enumerate(jobs)))
        self.msgs.put(("finished", results.count("ok"), results.count("fail"), self.cancelled))

    def _set_pct(self, src, pct):
        self.job_pct[src] = pct
        self.msgs.put(("progress", sum(self.job_pct.values()) / len(self.job_pct)))
        self.msgs.put(("row", src, f"Converting… {pct:4.0f}%", "run"))

    def _target_path(self, src, ext, outdir):
        name = os.path.splitext(os.path.basename(src))[0]
        folder = outdir or os.path.dirname(src)
        dst = os.path.join(folder, f"{name}.{ext}")
        if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(src)):
            dst = os.path.join(folder, f"{name} (converted).{ext}")
        with self.lock:   # parallel jobs must not pick the same output name
            root, e = os.path.splitext(dst)
            i = 0
            while os.path.exists(dst) or dst in self.reserved:
                i += 1
                dst = f"{root} ({i}){e}"
            self.reserved.add(dst)
        return dst

    def _convert_one(self, i, src, total, ext, quality, outdir, show_status):
        if self.cancelled:
            self.msgs.put(("row", src, "Cancelled", "err"))
            return "cancel"
        dst = self._target_path(src, ext, outdir)
        self.msgs.put(("row", src, "Starting…", "run"))
        if show_status:
            self.msgs.put(("status", f"[{i + 1}/{total}]  {os.path.basename(src)}  →  {ext.upper()}"))
        info = probe_info(src)
        # Fastest first; if FFmpeg rejects an attempt (a stream that can't be
        # copied after all), fall back to re-encoding audio, then everything.
        attempts = []
        for copy in ("va", "v", ""):
            a = build_args(ext, quality, info, copy)
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
        self.job_pct[src] = 100.0
        self.msgs.put(("progress", sum(self.job_pct.values()) / len(self.job_pct)))
        return result

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
                elif kind == "finished":
                    _, done, failed, cancelled = msg
                    self.worker = None
                    self.go_btn.config(text="Convert")
                    self.add_btn.set_enabled(True)
                    if cancelled:
                        self.status.config(text=f"Cancelled  ·  {done} converted", fg=ERR)
                    elif failed:
                        self.status.config(text=f"{done} converted, {failed} failed", fg=ERR)
                        name, log = getattr(self, "_last_error", ("", ""))
                        messagebox.showwarning(
                            "Some files failed",
                            f"{failed} file(s) could not be converted.\n\nLast error ({name}):\n{log[-600:]}")
                    else:
                        self.status.config(text=f"All done  ·  {done} file(s) converted", fg=OK)
        except queue.Empty:
            pass
        self.after(80, self._poll)


if __name__ == "__main__":
    App().mainloop()
