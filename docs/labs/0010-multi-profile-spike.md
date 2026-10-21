# Lab notebook 0010 — In-process multi-profile probe (Path B)

- **Issue:** [#10 — Architecture spike: Path B in-process multi-profile probe](https://github.com/Jvcon/qutebrowser/issues/10)
- **Date:** 2026-10-21
- **Verdict:** **FEASIBLE-WITH-CAVEATS**
- **Script:** [`scripts/dev/multi_profile_probe.py`](../../scripts/dev/multi_profile_probe.py)

## Question

Can several `QWebEngineProfile` instances with distinct persistent storage
paths coexist inside one `QApplication` in one process, while keeping cookies
and local storage isolated, containing failures to the affected profile, and
at an acceptable memory cost?

This is a spike only. No file under `qutebrowser/` or `tests/` was changed.

## Method

The script runs one `QApplication` and, by default, three named
`QWebEngineProfile` objects. Each profile gets a different
`setPersistentStoragePath()` and `setCachePath()`, plus one `QWebEngineView`.
A dependency-free `ThreadingHTTPServer` on `127.0.0.1` serves a tiny HTML page,
so the probe needs no external network. Profile 0 sets a cookie and a
`localStorage` key through JavaScript; the script then checks both the live
DOM and the on-disk profile directories.

The destructive ordering probes run in short-lived child interpreters because
the expected result is a process abort. Profile isolation, memory measurement,
and renderer-crash containment run in the single main `QApplication` process.
Qt runs headless with `QT_QPA_PLATFORM=offscreen` and
`QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox --disable-gpu --disable-dev-shm-usage`.

Reproduce with:

```console
.venv/bin/python scripts/dev/multi_profile_probe.py
```

The script prints `VERDICT: FEASIBLE`, `FEASIBLE-WITH-CAVEATS`, or
`NOT-FEASIBLE`, and exits 0, 2, or 3 respectively (3 = required check could
not run).

## Results (actual run)

Environment: Python 3.12.3, Qt 6.11.2 (compiled 6.11.0), PyQt 6.11.0,
`QT_QPA_PLATFORM=offscreen`.

### Storage and cookie isolation

| Profile | Cookie visible in page | `localStorage` visible | Cookie rows in `Cookies` DB | Storage marker on disk |
| --- | --- | --- | --- | --- |
| profile-0 | `spike_cookie=profile-0` | `profile-0` | 1 | yes |
| profile-1 | `''` | `null` | 0 | no |
| profile-2 | `''` | `null` | 0 | no |

All three pages loaded successfully. Runtime isolation, Cookies-database
isolation, and the best-effort localStorage raw-file marker scan all passed.
The disk checks used `PersistentCookiesPolicy.ForcePersistentCookies` and a
clean application shutdown; see the caveat below.

### Resident memory

RSS includes the Python/Qt application process plus all QtWebEngine child
processes, sampled three times after a settle period; values below are the
median sample (the three samples were identical at this precision).

| Configuration | Total RSS | Self | Children | Processes |
| --- | ---: | ---: | ---: | ---: |
| 1 profile + 1 page | 439.6 MiB | 246.0 MiB | 193.6 MiB | 4 |
| 3 profiles + 1 page each | 639.4 MiB | 266.2 MiB | 373.2 MiB | 6 |
| **Delta for 2 extra profiles** | **199.8 MiB** | — | — | — |

Marginal cost in this configuration: about **99.9 MiB per additional profile
with one loaded page**. The child-process count grew by two for the two extra
profiles, consistent with one additional renderer process per profile in this
Qt version. This is the cost of adding profiles on top of an already warmed-up
application; it is not a measurement of a second qutebrowser process.

### Failure isolation

The script navigated profile-2's page to `chrome://crash`. The renderer
terminated with `CrashedTerminationStatus` and exit code 139. Profile-0 and
profile-1 remained usable: profile-0 still returned
`spike_cookie=profile-0`, and profile-1 still returned an empty cookie. The
application process itself survived. Renderer-level failure containment passed.

## Constraints discovered

1. **Import order.** `QtWebEngineWidgets` must be imported before the
   `QApplication` is constructed (or `Qt.AA_ShareOpenGLContexts` must be set).
   Importing it afterwards raised:
   `ImportError: QtWebEngineWidgets must be imported or Qt.AA_ShareOpenGLContexts must be set before a QCoreApplication instance is created`
   (exit 1).
2. **Profile creation order.** Constructing a `QWebEngineProfile` before any
   application object aborted with `SIGABRT`; constructing one after a
   `QCoreApplication` but before a `QApplication` died with `SIGSEGV`. A
   control run that created `QApplication` first, then the profile and view,
   exited 0. Treat "create profiles only after `QApplication` exists" as a
   hard rule.
3. **Disk flush timing.** With `ForcePersistentCookies`, the cookie did not
   appear in the profile's `Cookies` SQLite database until the event loop was
   allowed to exit normally and the profile was torn down. The on-disk cookie
   assertion is therefore a clean-shutdown assertion, not a crash-recovery
   assertion. A hard exit may leave the database empty even though the cookie
   is live in the running profile.
4. **Headless flags.** QtWebEngine started under `offscreen` only with
   `--no-sandbox`, `--disable-gpu`, and `--disable-dev-shm-usage`. Without
   them this container is not a useful test bed. Qt still printed
   Vulkan/GPU warnings; they were non-fatal.
5. **Unique names and paths.** Each profile got a unique storage name and
   distinct storage/cache paths. Sharing either would defeat the isolation
   being measured.

## What this means for Phase 2

The in-process model is viable: storage is isolated, the cookie DB did not
leak across profiles, and a renderer crash in one profile did not take down
the other pages or the application. The main costs are architectural rather
than fundamental:

- profile creation must be sequenced after `QApplication`, which constrains
  early startup code and any lazy profile factory;
- each profile brings its own renderer and roughly 100 MiB RSS with one page
  loaded in this configuration, so "many profiles" is not free;
- cookie durability depends on a clean shutdown, so crash recovery must not
  assume the Cookies DB is current;
- headless deployment needs explicit Chromium flags.

Path B should therefore be treated as feasible with these constraints
documented in the profile-factory design, rather than as a drop-in change.

## Limitations / not verified

- Only Qt 6.11.2 / PyQt 6.11.0 on Linux was exercised. The process model and
  memory numbers may differ on macOS, Windows, or other Qt versions.
- One page per profile was tested. Many tabs per profile, background timers,
  service workers, downloads, permissions, and long-running cache growth were
  not measured.
- The failure test was a renderer crash (`chrome://crash`). A storage-layer
  failure, profile destruction while pages are alive, and out-of-memory
  behaviour were not tested.
- The localStorage on-disk check is a best-effort raw byte scan before
  shutdown, not a LevelDB query.
- The cookie was set by page JavaScript, not by an HTTP `Set-Cookie` response
  header. Both should use the same store, but only the JavaScript path was
  measured.
- The memory numbers include Python, Qt, and all QtWebEngine helper processes;
  they are not a per-profile heap attribution.

No bug in existing qutebrowser code was found or fixed. The known global
profile singleton and hard-coded profile selection in
`qutebrowser/browser/webengine/webenginesettings.py` were not touched; they
remain the refactor target if Path B is pursued.

## References

- Qt 6 `QWebEngineProfile` documentation — fetched 2026-10-21 via
  <https://doc-snapshots.qt.io/qtwebengine/qwebengineprofile.html>
  (canonical: <https://doc.qt.io/qt-6/qwebengineprofile.html>). Confirms that
  the storage name must be unique, that `persistentStoragePath()` holds
  persistent cookies and HTML5 local storage, and that a disk-based profile
  should be destroyed on or before application exit so cache and persistent
  data are flushed.
- qutebrowser issue [#519 — Multiple instances using profiles](https://github.com/qutebrowser/qutebrowser/issues/519)
  (checked via `gh`, closed): earlier profile/instance design discussion.
- qutebrowser issue [#4102 — Contextual identities / Firefox containers](https://github.com/qutebrowser/qutebrowser/issues/4102)
  (checked via `gh`, open): multi-context motivation and the "cannot switch a
  tab's profile after opening" constraint.
- qutebrowser issue [#5935 — Refactor profile initialization](https://github.com/qutebrowser/qutebrowser/issues/5935)
  (checked via `gh`, open): proposed profile factory and removal of the
  `default_profile` / `private_profile` globals; relevant to a Path B
  implementation.
- PyQt6 import-ordering requirement: **UNVERIFIED — from memory, not
  fetched.** The exact error text above is nevertheless reproduced by the
  script's `widgets-import-after-app` timing probe in this environment.
- Qt's "profile requires a `QApplication`, not merely a `QCoreApplication`"
  requirement: **UNVERIFIED — from memory, not fetched.** The observed
  `SIGABRT` / `SIGSEGV` results in the timing probes are the primary evidence.
