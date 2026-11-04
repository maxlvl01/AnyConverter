# Changelog

All notable changes to AnyConverter are listed here, newest first. Each version's section is also the text of its
GitHub release, where the downloads are.

## [Unreleased]

## [1.0]

The first release: portable builds for Windows and Linux, nothing to install.

### Added
- **Report a bug, right from the app.** The new "Report a bug" button at the top opens a small window: write what happened
  and press Send. AnyConverter adds its version, your system, the formats you're converting and the end of its log (your
  file names and user name are left out) and posts it as a public issue on GitHub. Without internet it goes the next time
  you start the app. Nothing is ever sent unless you press Send.
- **Convert audio and video** in batches, with quality presets. If FFmpeg is missing, the app offers to install it for you.
- **Convert much more than audio and video:** images (it also reads iPhone photos, PSD and TGA), PDFs to images or text,
  images to PDF, several files combined into one PDF, Word, PowerPoint and Excel files through LibreOffice (offered for
  install like FFmpeg), CSV, TSV, JSON and XLSX between each other, subtitles (SRT, VTT, ASS) and archives (ZIP, TAR).
- **The format list only shows what your files can become.**
- **Limit a video's size** with `--max-size 1280x720`: it's scaled down to fit, keeps its shape and is never made bigger.

### Changed
- **Conversions are much faster.** Files that already fit the new format are copied instead of re-encoded, which is close
  to instant. OGV and WebM videos are encoded in parallel slices (a 3:21 video to OGV went from about 6 minutes to 40
  seconds), and several audio files convert at the same time.

### Fixed
- **LibreOffice could hang.** Text export got stuck on some Word files with tables, and a stuck LibreOffice blocked every
  document after it. Files now go through a safer route to text, and timeouts and Cancel really stop LibreOffice.
- **Installing FFmpeg or LibreOffice failed** when the Microsoft Store source was broken on the PC.
