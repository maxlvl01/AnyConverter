# AnyConverter

Convert audio, video, images, PDFs, documents, spreadsheets, subtitles and archives into the format you want. Add your
files, pick a format and press Convert. It runs on **Windows** and **Linux**, and the downloads are portable: nothing to
install.

## Download

Get the newest build from the [Releases page](https://github.com/maxlvl01/AnyConverter/releases/latest):

- **Windows:** `AnyConverter.exe`. Double-click it.
- **Linux:** `AnyConverter-linux-x86_64.tar.gz`. Unpack it and run `AnyConverter`. Run `./install_linux.sh` from the
  folder to add it to your app menu (`--uninstall` removes it again). It needs glibc 2.35 or newer (Ubuntu 22.04 and later,
  or anything as recent).

Audio, video and subtitles are converted with [FFmpeg](https://ffmpeg.org), and documents, slides and spreadsheets with
[LibreOffice](https://www.libreoffice.org). Neither is bundled (they're big). If one is missing, AnyConverter explains it
and installs it for you with one click (winget on Windows, your package manager on Linux).

## What it converts

| You add | It can become |
|---|---|
| **Video** (mp4, mkv, mov, webm, avi, m4v, flv, wmv, mpg, 3gp, ts, ogv, gif …) | Any video format, any audio format, or an animated GIF |
| **Audio** (mp3, wav, flac, m4a, aac, ogg, opus, wma, aiff, alac, amr …) | mp3, wav, flac, m4a, aac, ogg, opus, wma, aiff |
| **Images** (png, jpg, webp, avif, bmp, tiff, ico, heic, psd, tga …) | png, jpg, webp, avif, gif, bmp, tiff, ico, pdf |
| **PDF** | One image per page (png, jpg, webp, tiff), or text. Several PDFs and images can be combined into one PDF |
| **Documents** (docx, doc, odt, rtf, txt, html) | pdf, docx, odt, rtf, txt, html |
| **Slides** (pptx, ppt, odp) | pdf, pptx, odp |
| **Spreadsheets** (xlsx, xls, ods) | pdf, xlsx, ods, csv |
| **Data** (csv, tsv, json, xlsx) | csv, tsv, json, xlsx |
| **Subtitles** (srt, vtt, ass, ssa) | srt, vtt, ass |
| **Archives** (zip, tar, tar.gz, tar.xz, tar.bz2) | zip, tar.gz, tar.xz, tar |

The "Convert to" list only shows formats at least one of your files can become. Convert as many files at once as you like.

- **Quality:** High, Medium or Small.
- **Fast:** streams that already fit the new format are copied instead of re-encoded (close to instant), and OGV and WebM
  videos are encoded in parallel slices.
- **Save to:** next to each file, or in a folder you pick. Existing files are never overwritten.
- **Cancel** stops everything that's running, including LibreOffice.

## Command line options

```
AnyConverter [--format EXT] [--max-size WxH] [FILE ...]
```

- `--format EXT` preselects the target format.
- `--max-size WxH` scales videos down to fit (shape kept, never made bigger).
- Any files listed are added to the list right away.

For example `AnyConverter --format ogv --max-size 1280x720 clip.mp4`. [Quiznight?](https://github.com/maxlvl01/quiznight)
opens AnyConverter this way.

## Found a bug?

Press **Report a bug** at the top of the app, write what happened and press Send. It's posted as an issue on this repo,
together with the app's version, your system, the formats you were converting and the end of its log. Your file names,
folders and user name are left out, but the issue is public, so keep private details out of what you write. Nothing is sent
unless you press Send. You can also [open an issue](https://github.com/maxlvl01/AnyConverter/issues) yourself.

## Run it from source

You need Python 3.12 or newer with tkinter (on Linux the `python3-tk` package).

```bash
pip install pillow pillow-heif pypdfium2 openpyxl
python anyconverter.py        # on Windows: py anyconverter.py
```

Images, PDFs and data files need those packages; the downloads already contain them. Run from source, the app can't send bug
reports (the key for that is only in the downloads).

To build the portable app yourself: `./build_linux.sh` on Linux makes `dist/AnyConverter`. The Windows and Linux downloads
on the Releases page are built by [GitHub Actions](.github/workflows/build.yml) with PyInstaller. Pushing a tag like `v1.0`
builds both and publishes the release.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE). FFmpeg and LibreOffice are separate programs with their own licenses; they aren't part of this repo or the
downloads.
