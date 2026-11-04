# AnyConverter — project notes for Claude

A desktop app that converts audio, video, images, PDFs, documents, spreadsheets, subtitles and archives. Python + tkinter,
for Windows and Linux. The whole app is one file, [anyconverter.py](anyconverter.py); keep it that way. Written for people
who aren't technical: texts on screen are plain and friendly, and a missing tool gets a guide with one-click install
(`ToolDialog`), not an error.

**Quiznight?** (`maxlvl01/quiznight`) ships this app in its release zips and opens it for videos, so don't break its
arguments: `AnyConverter --format ogv --max-size 1280x720 FILE` (`App._apply_args`).

## How it works

- **Formats:** `KINDS` says which extensions are read as what and what each kind can become, `FORMAT_GROUPS` what the
  "Convert to" list shows, `engine_for(path, target)` picks the converter. A new format usually only touches these tables.
- **Engines:** `ffmpeg` (audio, video, subtitles), `image` (Pillow), `pdf` (pypdfium2), `data` (csv/tsv/json/xlsx with
  openpyxl), `office` (LibreOffice), `archive` (zipfile/tarfile). Each is an `App._conv_<engine>` method, except ffmpeg
  (`_convert_media`). Outside programs are found by `detect_ffmpeg`/`detect_office` and offered for install through `TOOLS`.
- **Threads:** conversions run on `self.worker` and pool threads. **Only the main thread touches tkinter**: workers put
  messages on `self.msgs` and `_poll` handles them every 80 ms. That includes reading `StringVar`s, so values go to workers
  as arguments.
- **Speed:** streams that fit the target are copied, not re-encoded (`COPY_VIDEO`/`COPY_AUDIO`, not on "Small"), with
  fallbacks if FFmpeg refuses (`_convert_media`). libtheora and libvpx-vp9 can't fill a modern CPU, so `_parallel_video` cuts
  the video into time slices on one shared frame grid and joins them losslessly.
- **LibreOffice:** one at a time (`OFFICE_LOCK`, shared profile). soffice leaves `soffice.bin` running, so stop it with
  `kill_tree`, never `terminate` alone. Text export can hang on Word tables, so `.txt` goes through `.odt` first.

## Bug reports

The "Report a bug" button (`BugDialog`) opens one text box, and Send makes a GitHub Issue in this repo through the API:
title `<category> (<version>)`, label `bug`, the log folded in `<details>`. The category is the formats on screen
(`mkv, mp4 → webm`) or `General`. It's modelled on Quiznight's `Storage/BugReports.cs`, without the screenshot (a token that
can only write issues has nowhere to put one) and without its ntfy.sh push.

- **Offline:** a report that can't go waits in `WAITING_REPORTS` as JSON and goes on the next start (`send_waiting_reports`).
- **Log:** there's no log file. `LOG` holds the last 200 lines in memory; use `App._log`, which leaves out file names, folders
  and the user name as it writes (`anonymise`), so names stay out even after the list is cleared. Plain `log()` is only for
  text that can't hold a path. Errors in buttons and threads land there too, because built copies have no console.
- **Nothing is sent unless the user presses Send, and this repo is public, so reports are too** (Quiznight's go to a private
  repo). The dialog says so. Don't add file names to `report_details`.
- **Token:** `report-token.txt` (gitignored, never commit it) holds a fine-grained GitHub token with only "Issues: read and
  write" on this repo. Anyone can pull it out of a public build, so that's all it may ever be able to do; if it's abused,
  revoke it and release a build with a new one. GitHub Actions writes it from
  the repo secret `REPORT_TOKEN` and bundles it with `--add-data`; the build stops if the secret is missing. Runs from source
  use the file if it's next to the script, otherwise the dialog says this copy can't send.

## Commands

On Windows use `py`, or the full path: Python 3.12 was installed for the user with winget, so it may not be on PATH yet.

```bash
pip install pillow pillow-heif pypdfium2 openpyxl    # what images, PDFs and data files need
py anyconverter.py                                   # run it (Linux: python3, plus the python3-tk package)
py anyconverter.py --format webm --max-size 1280x720 FILE
./build_linux.sh                                     # dist/AnyConverter
```

There are no automated tests. Real builds come from GitHub Actions (`.github/workflows/build.yml`): Actions → Build → Run
workflow for a test build, whose files are on the run's page. It builds on Ubuntu 22.04 (older = runs on more distros) with
PyInstaller `--onefile --windowed` and starts the Linux build once on a virtual screen as a smoke test.

**Releases:** add changelog entries under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) while working (plain language, what
changes for the user). For a release, rename that section to the version, bump `VERSION`, then push a tag with that version
(`v1.0`). The workflow publishes a GitHub release with both portable builds, `AnyConverter.exe` and
`AnyConverter-linux-x86_64.tar.gz`, and that section as its text; it stops if the section is missing. Linux builds only come
from there (no WSL here).

## Gotchas

- **`--windowed` builds have no console:** `sys.stderr` is None there, so anything only printed is lost. Put it in the log.
- **Windows:** every subprocess needs `creationflags=NO_WINDOW`, or a black window flashes. `_fresh_path()` reads PATH from
  the registry so a tool just installed with winget is found without a restart. winget commands keep `--source winget`: a
  broken Microsoft Store source otherwise makes winget stop and ask.
- **Linux:** file names are case-sensitive (`_patterns` also matches `VIDEO.MP4`), and install commands run in a visible
  terminal (`open_in_terminal`) so the user can type a password.
- **`anyconverter.py` has CRLF line endings;** `.sh` and `.yml` stay LF (`.gitattributes`).
- **Bump `VERSION`** for a release: bug reports are filed under it.
- **Quiznight's `release.sh` looks for the builds in `Desktop\AnyConverter\dist`,** which no longer exists: set
  `ANYCONVERTER=` and `ANYCONVERTER_LINUX=` to the downloaded builds.
- Never `git push` without the user's approval for that push.
