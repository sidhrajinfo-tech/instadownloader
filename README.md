# Instagram Public Media Downloader

A Streamlit dashboard for archiving **public Instagram posts you have permission to use**, with Instaloader, background processing, cooperative cancellation, resume support and ZIP export.

## Installation

Use Python **3.10 or newer**. Extract the ZIP, open a terminal in `instagram_downloader`, then run:

```bash
python -m venv venv
```

Windows Command Prompt:

```bat
venv\Scripts\activate
```

Windows PowerShell:

```powershell
.\venv\Scripts\Activate.ps1
```

macOS / Linux:

```bash
source venv/bin/activate
```

Install and start:

```bash
pip install -r requirements.txt
streamlit run app.py
```

If the `streamlit` command is unavailable, use `python -m streamlit run app.py`.
Open the local URL printed by Streamlit, usually http://localhost:8501.

## Using the app

1. Enter a username such as `example_account`; no `@` is required (one leading `@` is accepted).
2. Select **Check Profile** to show available profile details. A private profile is rejected again by the download worker, even if the profile was not checked first.
3. Choose 100, 250, 500 or 1000 posts, or **Custom** for 1–1000.
4. Choose **Images only** or **Images + Videos** and whether to write metadata.
5. Enter an output **folder name**, then select **Start Download**.
6. Follow live counters and select **Stop Download** whenever necessary.
7. After completion, stopping, or an access failure, select **Download ZIP** to retrieve preserved files.

The worker runs independently of Streamlit's UI. A one-second fragment refresh updates the dashboard. Changing settings is disabled during a job. `st.session_state` retains the job, settings, status, progress, statistics, stop state and errors through ordinary reruns. The worker uses a lock and cancellation event; it never calls Streamlit APIs or mutates session state directly.

### What the counters mean

- **Posts processed**: distinct posts encountered in this run whose processing ended, including failures, previously saved posts and video-only posts excluded by the filter.
- **Successful**: all selected media for that post are present, with requested metadata saved. Includes fully reused posts.
- **Failed**: a processed post has a media or metadata failure. Successfully saved media in that post are retained.
- **Images / Videos downloaded**: newly saved media files this run, excluding reused files.
- **Posts already on disk**: successful posts requiring no new media download.
- **No matching media**: video-only posts in Images-only mode. These still count toward the requested post limit; no video thumbnails are substituted.

**100 posts means 100 posts, not 100 images.** A carousel may contribute several files but only one processed post. The limit applies to the first N posts supplied by Instagram, including pinned posts in Instagram's ordering. It is not a promise of N successful downloads. If the account has fewer accessible posts, processing ends at exhaustion and the requested-limit progress bar can remain below 100%. An interrupted current post may have preserved files without yet contributing to the processed count.

## Output layout

By default, files are stored beneath this project's `downloads` directory:

```text
downloads/
  Instagram_Downloads/
    example_account/
      2026-09-25_ABC123/
        image_01.jpg
        image_02.jpg
        video_03.mp4
        metadata.json
      download_index.json
      download_summary.json
  _archives/
    <unique-job-id>.zip
```

The UI's default output folder is `Instagram_Downloads`. It is deliberately a single safe folder name, not an arbitrary filesystem path. The administrator can change the base directory using `INSTAGRAM_DOWNLOAD_ROOT`. Captions never become filenames. Path traversal, symlink escapes and Windows reserved-name conflicts are handled. Do not allow untrusted operating-system users to modify the archive directory concurrently.

Metadata includes shortcode, UTC date, caption, public like/comment counts when available, canonical post URL, video flag, and total carousel media count. Counts can be null when unavailable. No comment bodies, geotags or follower lists are collected. Metadata is a snapshot at initial archive time; resuming does not refresh existing metadata. Disabling metadata prevents new metadata files; it does not delete earlier metadata.

## Stop and resume

**Stop Download** prevents new work and interrupts request-spacing waits. A current HTTP operation may need to finish or time out first (30-second network timeout; a streaming transfer can take longer while data continues arriving). Committed files are preserved. ZIP packaging finishes before the final stopped state is displayed.

For example, after downloading 100 posts, request 500 using the **same username and output folder**. The app re-enumerates the first 500 currently accessible posts and checks an atomic JSON index. Completed media with the recorded nonzero size are reused; missing or size-mismatched files are downloaded again. A later Images + Videos run adds missing videos. Each media file is downloaded into temporary storage and committed only after completion; the index is persisted after each media file.

Resume compares names and sizes, not cryptographic hashes. An edited file of the same size is not detected. If the process is killed between committing media and its index, that one file may be downloaded again. A hard-killed process may leave `.partial-*` temporary folders, which are excluded from ZIPs and can be removed when no job is running.

Resume does not bypass Instagram: listing posts still makes requests and may be denied. New/deleted/pinned posts can change which first N posts are returned. Older archived posts are retained.

A folder lock prevents concurrent writers, including separate app processes. If the process crashes, confirm **no worker or app process is using the folder** before manually deleting its `.download.lock`. Corrupt/unsupported indexes stop safely; restore a known-good backup or choose a new output folder.

## ZIP downloads and opening folders

ZIPs include the complete saved account folder, including earlier runs, metadata, index and summary. Internal locks and temporary files are excluded. Browser download filenames are `Instagram_Download_username.zip`. ZIPs use ZIP64 and store already-compressed media without additional compression. The archive is built on disk, outside the account folder.

Streamlit holds browser downloads in memory. To avoid loading multi-gigabyte archives unexpectedly, direct ZIP downloads default to a **512 MiB** limit. For larger files, the app displays the completed ZIP's server path. Raise `INSTAGRAM_MAX_ZIP_MB` only when enough RAM is available, or retrieve that ZIP directly from the server. This is a deployment resource limit, not a post-count limit. Allow space for both the media and its ZIP; 1000 video posts can be very large. ZIP snapshots remain on disk until an operator removes them.

**Open Download Folder** is enabled only when `INSTAGRAM_ENABLE_OPEN_FOLDER=1` is set before starting Streamlit. It opens the **server's desktop**, so enable it only when running on your own desktop machine. Remote hosting cannot open the visitor's local folder. This action never invokes a shell with user-supplied text.

Example configuration (PowerShell):

```powershell
$env:INSTAGRAM_DOWNLOAD_ROOT = "D:\InstagramArchives"
$env:INSTAGRAM_ENABLE_OPEN_FOLDER = "1"
$env:INSTAGRAM_MAX_ZIP_MB = "1024"
streamlit run app.py
```

## Access limits and common errors

**Public does not guarantee anonymous access.** Instagram may require login for profile lookup or post enumeration, return 401/403/429, or change its endpoints. This application intentionally cannot solve those restrictions. It does not request credentials, load authenticated sessions, bypass CAPTCHA/login, rotate proxies or access private posts. It may therefore be unable to download even a public account at a given time.

- **Profile not found**: verify the username; it may have been renamed/deleted.
- **Private account**: only public content is supported; no media is fetched.
- **Access temporarily limited or login required**: the job stops safely. Wait and try later manually; do not repeatedly restart. There is no automatic retry loop.
- **Deleted post / failed media URL**: log the failure, retain completed media, continue the remaining media/posts when access is still available.
- **Network failure**: individual media failures can be skipped. A post-list iterator failure ends the job safely because that iterator cannot reliably be advanced; rerun later to resume.
- **Storage full / permissions**: preserve completed files and stop for common local disk errors. Free space or correct permissions before resuming.
- **ZIP unavailable**: completed media remain on disk even if packaging fails.
- **Damaged index**: restore a backup or use a new folder. Do not edit an index during a download.

The app spaces post and media operations by five seconds, uses Instaloader's own rate controller, and permits only one connection attempt per operation. HTTP 429 and access restriction signals stop the job without repeatedly retrying. No request speed is guaranteed to avoid Instagram restrictions. Available diagnostics show error types and safe explanations rather than raw tracebacks, signed media URLs or captions.

## Deployment and operations

This project is intended for **one trusted operator**, locally or on an authenticated private server. It is not a public multi-tenant service: account folders are shared within the configured storage root, and there is no built-in user authentication or per-user filesystem isolation. If hosting remotely, place it behind authentication/TLS and use a persistent volume. Give the app access only to its archive directory, provision disk/RAM quotas, and back up indexes with their media. Do not disable Streamlit's normal XSRF protections.

Jobs survive ordinary Streamlit reruns, but not process restarts. A disconnected/reloaded browser may lose its session's in-memory job handle; the worker can continue while the server is alive. Do not launch another job into the locked folder. Reconnect/resume after it finishes, or stop the server to terminate an orphaned worker. Durable distributed queues and cross-session job recovery are outside this single-operator implementation. Streamlit's `runner.enforceSerializableSessionState` must remain at its default `false` because the job holds thread primitives.

Set positive integer values for `INSTAGRAM_MAX_ZIP_MB`. Restart the app to apply environment changes. Periodically remove obsolete `_archives/*.zip` snapshots while no downloads are using them. Browser-download RAM also depends on simultaneous sessions; 1000-video archives may need server-side retrieval.

## Validation

```bash
python -m compileall -q app.py downloader.py
python -m unittest discover -s tests -v
```

The included offline tests cover carousel counting, resume/media-mode upgrades, ZIP integrity, missing-file recovery, media failures, cancellation, private-account rejection, rate-limit stops, iterator failure, folder locks and safe paths. Development validation used Python 3.12.14, Streamlit 1.64.0 and Instaloader 4.15.3. Requirements specify compatible minimum versions rather than an obsolete exact pin; validate dependency upgrades in your deployment.

Live Instagram availability is not guaranteed or simulated as a successful real download. The application depends on Instagram's current anonymous endpoints and their restrictions.

## Legal and copyright

Only download content you are authorized to archive or use. Public visibility is not a copyright license or permission to redistribute. Respect creators' rights, applicable privacy laws and Instagram's applicable terms. Obtain permission when required and securely delete archives you no longer need.

## Project files

- `app.py`: Streamlit UI, session state and polling dashboard.
- `downloader.py`: Instaloader access, worker, cancellation, safe paths, resume and ZIP export.
- `requirements.txt`: the two direct runtime dependencies.
- `tests/test_downloader.py`: offline regression tests using Python's standard library.
- `downloads/`: default archive storage, excluded from Git.

API references: [Instaloader](https://instaloader.github.io/module/instaloader.html), [Streamlit fragments](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment), [Streamlit downloads](https://docs.streamlit.io/develop/api-reference/widgets/st.download_button).
