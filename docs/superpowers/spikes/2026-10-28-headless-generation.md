# Spike: does Flow generate images when real Chrome runs headless?

**Date:** 2026-10-28 (headless run 6 and the headed re-check on 2026-10-29) · **Cost:** 0 credits
(text-to-image only) · **Script:** `scripts/dev/spike_headless_generation.py` (identity probe, no submit)

## Question
Several places claimed that "reCAPTCHA Enterprise rejects headless with an immediate 403"
(`src/gflow_cli/config.py`, `docs/CONFIGURATION.md`, `docs/ACCOUNT_SAFETY.md`,
`docs/AUTHENTICATION.md`), but none cited a measurement, and
[the G12 spike](2026-09-08-g12-blocks-webdriver-not-playwright.md) § Not measured says headless
"says nothing in either direction". The question: with `GFLOW_CLI_HEADLESS=true`, gflow launches
real Chrome (`channel=chrome`, the same saved profile) headless. Does Flow on flow.google.com
accept an image generation from it?

The answer decides whether a fully hidden browser on Windows and macOS is worth building. The
offscreen window takes keyboard focus when it opens, and there is no macOS-style
background-only mode on Windows.

## Method
- One test profile (`thu-headless`), one existing Flow project, served flow.google.com, macOS,
  Chrome 156, gflow 0.90.0 from this branch. No code changes: the CLI already passes
  `settings.headless` to the client.
- `gflow image t2i "<prompt>" --profile thu-headless --project <id> -o <file>` once headed
  (baseline), then six times with `GFLOW_CLI_HEADLESS=true`, each with a different prompt, 1 to
  3 minutes apart. Then one more headed run as a re-check.
- Identity probe: the spike script loads Flow headed and headless and records
  `navigator.userAgent`, UA-CH brands and `navigator.webdriver`. It does not submit.
- Focus: `lsappinfo front` sampled every 0.1 s during runs 1 to 6. Run 6 and the re-check also
  record the front app's PID and whether that process is gflow's Chrome (its command line
  contains the gflow profile directory).

## Result

| Run | Mode | Exit | Time | Image | gflow's Chrome in front? |
|---|---|---|---|---|---|
| base | headed (`GFLOW_CLI_WINDOW_MODE=background`) | 0 | not recorded | yes | not sampled |
| h1 | headless | 0 | 35–48 s (h1–h5) | yes | no (front: other apps) |
| h2 | headless | 0 | | yes | no |
| h3 | headless | 0 | | yes | no |
| h4 | headless | 0 | | yes | no |
| h5 | headless | 0 | | yes | **unattributed**: all 103 samples read "Google Chrome", PID not recorded |
| h6 | headless | 0 | 37 s | yes | **no**, 0 of 279 samples (PID-attributed) |
| re-check | headed (`background`) | 0 | 49 s | yes | **no**, 0 of 371 samples (PID-attributed) |

All six headless runs generated an image. There was no 403, no `RecaptchaError` and no WAF
response. The headed re-check after them also succeeded, so this profile was not visibly marked.

Identity, as the page sees it:

| | User agent | UA-CH brands | `navigator.webdriver` |
|---|---|---|---|
| headed | `... Chrome/156.0.0.0 Safari/537.36` | Not:A-Brand, Chromium, Google Chrome | not set |
| headless | `... HeadlessChrome/156.0.0.0 Safari/537.36` | Not:A-Brand, Chromium, Google Chrome | not set |

**The headless arm advertises itself in the user agent** (`HeadlessChrome`). Flow still served
it, so the user agent alone does not cause an immediate block. It may still lower a reCAPTCHA
score in ways a single account over a few minutes cannot show.

Run h5 was sampled before the PID was recorded. The "Google Chrome" in front was most likely
the user's own browser: headless Chrome opens no window, and run h6, the same command
attributed by PID, never once showed gflow's Chrome in front. h5 is therefore left as
unattributed rather than counted either way.

## What this changes
- The "immediate 403" claim is **false for real Chrome headless on this account and surface**
  (images on flow.google.com). The docs and the `headless` setting description now say what was
  measured and link here, instead of asserting a rejection.
- Headed stays the default. Six runs on one account do not show how reCAPTCHA scores a headless
  browser over days, or on video.

## Not measured
- **Video.** Veo generation is billed and more tightly guarded; nothing here says headless video
  works.
- **Windows.** Every run was on macOS. Windows is where hiding the window matters most, because
  it has no background-only mode.
- **More than one account, and time.** One test profile over about ten minutes, then once the next
  day. A gradual drop in reCAPTCHA score would not show here.
- **Bundled Chromium headless.** gflow refuses to downgrade to bundled Chromium; only real Chrome
  was measured.
- **Hiding `HeadlessChrome` from the user agent.** Not attempted. It is the next step if
  headless becomes a supported window mode.

## Next
A `window_mode=headless` proposal goes through `/gflow:predict` first (auth/reCAPTCHA surface).
Before it ships it needs a headless video run (billed, ask first), a Windows run, and a decision
on overriding the `HeadlessChrome` user-agent token.
