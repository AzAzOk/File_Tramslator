# First PDF Converter — local DOCX conversion service

Standalone FastAPI service that converts PDF → DOCX on a Windows host by
driving the **First PDF 6.4** desktop application through Windows UI Automation
(`pywinauto`). It is a companion to the File Translator app: the main app stays
Docker-based, this service runs *on the same Windows machine* where the
interactive First PDF session lives.

> **Hosting model**: outside Docker. First PDF is a desktop WinForms/WPF app and
> needs an interactive desktop session to render its window. Do not try to run
> it in a container or a non-interactive service session — the window never
> appears there.

## Prerequisites

- Windows host with an **interactive (logged-on) desktop session**
- **First PDF 6.4** installed at the default path
  `C:\Program Files (x86)\First PDF 6.4\First PDF.exe`
  (override with `FIRST_PDF_EXE`)
- Python 3.12
- Install dependencies:

      pip install -r requirements.txt

  (`fastapi`, `uvicorn`, `python-multipart`, `pywinauto`)

## Manual start

From the repository root:

    python -m uvicorn first_pdf_converter.server:app --port 8001

Health check:

    curl http://127.0.0.1:8001/health        # {"status":"ok"}

Convert a PDF (multipart upload):

    curl -F "file=@input.pdf" http://127.0.0.1:8001/convert -o output.docx

The response body is the DOCX (`Content-Disposition: attachment`, filename
derived from the source stem). **Only first PDF in the file gets converted —
sequential processing is by design**: conversions are serialized with an
in-process single-flight lock (a second request waits for the first).

### Per-request timeout (`X-Timeout-Seconds`)

The watchdog deadline can be sized per request with an optional
`X-Timeout-Seconds` request header (sent by the File Translator's
`PdfToDocxConverter`, which estimates it from the PDF size — floor 30 s,
~3.7 s/MB, ×2 slack, capped by `FIRST_PDF_MAX_TIMEOUT`):

    curl -F "file=@big.pdf" -H "X-Timeout-Seconds: 600" \
         http://127.0.0.1:8001/convert -o output.docx

Effective timeout = `max(FIRST_PDF_TIMEOUT, min(client deadline, FIRST_PDF_MAX_TIMEOUT))`.
A missing/invalid header falls back to `FIRST_PDF_TIMEOUT`. This lets big PDFs
finish while still killing a genuinely hung First PDF process at the hard cap.

## Error responses (JSON `{ "code", "message" }`)

| HTTP | code              | when                                                        |
|------|-------------------|-------------------------------------------------------------|
| 400  | `BAD_REQUEST`     | missing/not-PDF/empty upload                                |
| 502  | `CONVERT_FAILED`  | First PDF can't start, or produced no DOCX                 |
| 504  | `CONVERT_TIMEOUT` | no DOCX within `FIRST_PDF_TIMEOUT` seconds                  |
| 500  | `INTERNAL`        | upload storage failure / unexpected error                   |

## Environment variables

| var                 | default                                              | meaning                                   |
|---------------------|------------------------------------------------------|-------------------------------------------|
| `FIRST_PDF_EXE`     | `C:\Program Files (x86)\First PDF 6.4\First PDF.exe` | executable path (must point at First PDF) |
| `FIRST_PDF_PORT`    | `8001`                                               | uvicorn port                              |
| `FIRST_PDF_WORK_DIR`| `%TEMP%\first_pdf_work`                              | per-request temp dirs root                |
| `FIRST_PDF_TIMEOUT` | `120` (seconds)                                      | default poll deadline for the output DOCX |
| `FIRST_PDF_MAX_TIMEOUT` | `3600` (seconds)                                 | hard cap for the `X-Timeout-Seconds` header |

## Task Scheduler autostart recipe

Task Scheduler is the recommended way to keep the service up (an interactive
session must be active where First PDF runs):

1. `Win+R` → `taskschd.msc` → *Create Task*.
2. **General**: name `first-pdf-converter`; *Run only when user is logged on*
   (required — UI automation needs you logged in); *Do not store password*;
   run with highest privileges only if First PDF requires it.
3. **Triggers**: *At startup* (or *At log on* — cleaner for per-user desktop
   sessions).
4. **Actions** → *New*:
   - Program/script: `C:\...\Python312\pythonw.exe`
   - Arguments: `-m uvicorn first_pdf_converter.server:app --port 8001`
   - Start in: `<repo root>`
5. **Conditions**: uncheck *Start the task only if the computer is on AC power*.
6. **Settings**: uncheck *Stop the task if it runs longer than*.

Restart the task after First PDF updates (selectors below are pinned to 6.4).

## How it works / buttons pinned to First PDF 6.4

`automation.py` launches First PDF with the PDF path, connects to the window
(auto-id `MainForm`), enables the custom output folder (toggle `CustomPath`,
textbox `TB_CustomPath`), clicks `Button_Convertion` and polls for the DOCX.
Every control change is **read back and verified** because First PDF silently
ignores a wrong custom path and writes next to the *source* file.
The output-format combo (`CBConvertionDirection`) is left on its default
(Word) — its items are unnamed in this build.

If a First PDF update renames any control, `automation.py` (module docstring +
`_set_custom_path`) is the only place that needs changing.

## Known limits & findings (validated 2026-09-08)

- **Trial watermark probe: NONE.** A 300-page / multi-page document converted
  with no `trial`/`watermark`/`first pdf` markers in any DOCX XML part. First
  PDF 6.4 trial did not embed watermarks or page caps in our test; re-check
  with `wm_scan.py`-style ZIP text scan if you rely on it.
- **8.3 short paths are silently ignored** by the custom-output textbox: always
  pass full (long) paths (`ensure_long_path` + `os.makedirs` first, because
  `GetLongPathNameW` can't expand a component that doesn't exist yet).
- The service must run in an **interactive session**; a headless/LSA session
  will fail at the `app.connect(backend="uia")` step.
- Only one First PDF instance can convert at a time (single-flight lock); the
  lock waits, it never queues across processes.
- Temp dirs are removed after each request (background cleanup with retries);
  `_kill_first_pdf` (`taskkill /F /T /IM`) always terminates the spawned
  First PDF tree on return — no stray processes are left.

## Troubleshooting

| symptom                                   | likely cause / fix                                   |
|-------------------------------------------|------------------------------------------------------|
| `502 CONVERT_FAILED: First PDF executable not found` | wrong `FIRST_PDF_EXE` path           |
| `502 CONVERT_FAILED: Could not configure First PDF output folder` | First PDF version renamed controls (see pinning) |
| `504 CONVERT_TIMEOUT` on a big PDF        | raise `FIRST_PDF_TIMEOUT` (default 120 s) or send a larger `X-Timeout-Seconds` (up to `FIRST_PDF_MAX_TIMEOUT`) |
| `500 INTERNAL: Unexpected conversion error` while converting a corrupt/`not-PDF` file | First PDF misbehaves on malformed input; the service stays up (safety net) |
| `ElementAmbiguousError` / focus errors    | ignore — the recipe never calls `set_focus()`        |
| PDF still lands in Downloads               | the output-path textbox saw an 8.3 path; use long paths |