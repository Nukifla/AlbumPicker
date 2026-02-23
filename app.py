import os
import re
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import tempfile
import shutil
import time
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string
import requests

app = Flask(__name__)

# ── In-memory state ──────────────────────────────────────────────────────────
state = {
    "server": None,       # base URL
    "token": None,        # auth token
    "user_id": None,
    "albums": [],
    "jobs": {},           # job_id -> {status, progress, log, total, track_status}
}

# ── Jellyfin helpers ─────────────────────────────────────────────────────────

def jf_headers():
    token = state["token"]
    return {
        "X-Emby-Token": token,
        "Content-Type": "application/json",
    }

def jf_url(path):
    return state["server"].rstrip("/") + path

def authenticate(server, username=None, password=None, api_key=None):
    server = server.rstrip("/")
    if api_key:
        # validate api key by hitting /Users endpoint
        r = requests.get(f"{server}/Users", headers={"X-Emby-Token": api_key}, timeout=10)
        r.raise_for_status()
        users = r.json()
        state["server"] = server
        state["token"] = api_key
        state["user_id"] = users[0]["Id"]
        return True
    else:
        payload = {"Username": username, "Pw": password}
        r = requests.post(
            f"{server}/Users/AuthenticateByName",
            json=payload,
            headers={"X-Emby-Authorization": 'MediaBrowser Client="JellyfinPicker", Device="WebApp", DeviceId="jellyfin-picker-001", Version="1.0.0"'},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        state["server"] = server
        state["token"] = data["AccessToken"]
        state["user_id"] = data["User"]["Id"]
        return True

def fetch_all_albums():
    uid = state["user_id"]
    albums = []
    start = 0
    limit = 500
    while True:
        r = requests.get(
            jf_url(f"/Users/{uid}/Items"),
            headers=jf_headers(),
            params={
                "IncludeItemTypes": "MusicAlbum",
                "Recursive": "true",
                "Fields": "Overview,Genres,ProductionYear,PremiereDate,ChildCount,CumulativeRunTimeTicks,UserData",
                "StartIndex": start,
                "Limit": limit,
                "SortBy": "SortName",
                "SortOrder": "Ascending",
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("Items", [])
        albums.extend(items)
        if start + limit >= data.get("TotalRecordCount", 0):
            break
        start += limit

    # Fetch all track-level play counts in one request and sum per album.
    # Jellyfin's album-level PlayCount is unreliable — track sum is accurate.
    try:
        track_plays = {}  # album_id -> total play count
        start = 0
        while True:
            r = requests.get(
                jf_url(f"/Users/{uid}/Items"),
                headers=jf_headers(),
                params={
                    "IncludeItemTypes": "Audio",
                    "Recursive": "true",
                    "Fields": "UserData",
                    "StartIndex": start,
                    "Limit": 1000,
                },
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            for track in data.get("Items", []):
                aid = track.get("AlbumId")
                plays = track.get("UserData", {}).get("PlayCount", 0) or 0
                if aid:
                    track_plays[aid] = track_plays.get(aid, 0) + plays
            if start + 1000 >= data.get("TotalRecordCount", 0):
                break
            start += 1000
        # attach to albums
        for a in albums:
            a["_TrackPlayCount"] = track_plays.get(a["Id"], 0)
    except Exception:
        for a in albums:
            a["_TrackPlayCount"] = 0

    return albums

def get_album_tracks(album_id):
    uid = state["user_id"]
    r = requests.get(
        jf_url(f"/Users/{uid}/Items"),
        headers=jf_headers(),
        params={
            "ParentId": album_id,
            "IncludeItemTypes": "Audio",
            "Recursive": "true",
            "Fields": "Path,IndexNumber,ParentIndexNumber",
            "SortBy": "ParentIndexNumber,IndexNumber,SortName",
            "SortOrder": "Ascending",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("Items", [])

def fmt_bytes(b):
    if b < 1024: return f"{b} B"
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"

def download_track(track, dest_dir, job=None, track_num=None, track_total=None):
    tid = track["Id"]
    name = re.sub(r'[<>:"/\\|?*]', '_', track.get("Name", tid))
    ext = ".audio"
    src_path = track.get("Path", "")
    if src_path:
        ext = Path(src_path).suffix or ".audio"
    filename = f"{track.get('IndexNumber', 0):03d}_{name}{ext}"
    dest = os.path.join(dest_dir, filename)
    if os.path.exists(dest):
        if job:
            job["log"].append(f"    [{track_num}/{track_total}] ✓ cached: {name}{ext}")
            job["track_status"] = ""
        return dest

    url = jf_url(f"/Items/{tid}/Download")
    with requests.get(url, headers=jf_headers(), stream=True, timeout=300) as resp:
        resp.raise_for_status()
        total_size = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        start_t = time.time()
        last_log_t = 0

        with open(dest, "wb") as f:
            for chunk in resp.iter_content(4 * 1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                elapsed = max(now - start_t, 0.001)
                speed = downloaded / elapsed

                if job:
                    pct = f" ({downloaded/total_size*100:.0f}%)" if total_size else ""
                    job["track_status"] = (
                        f"[{track_num}/{track_total}] ⬇ {name}{ext}  "
                        f"{fmt_bytes(downloaded)}"
                        + (f"/{fmt_bytes(total_size)}" if total_size else "")
                        + pct
                        + f"  @ {fmt_bytes(speed)}/s"
                    )
                    if now - last_log_t >= 3.0:
                        job["log"].append(f"    {job['track_status']}")
                        last_log_t = now

    elapsed = max(time.time() - start_t, 0.001)
    speed = downloaded / elapsed
    if job:
        job["log"].append(
            f"    [{track_num}/{track_total}] ✓ {name}{ext}  "
            f"{fmt_bytes(downloaded)}  avg {fmt_bytes(speed)}/s"
        )
        job["track_status"] = ""
    return dest

def ticks_to_duration(ticks):
    if not ticks:
        return "?"
    seconds = ticks // 10_000_000
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"

def safe_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip()

# ── Background export job ────────────────────────────────────────────────────

# How many CPU cores to use for parallel ffmpeg conversions
import os as _os
CPU_CORES = max(1, (_os.cpu_count() or 4))
# How many tracks to download simultaneously (per album)
DL_WORKERS = 8

def convert_track(src, out_path, fmt, job, wi, total):
    """Convert a single downloaded track to normalised PCM WAV or CBR MP3."""
    if os.path.exists(out_path):
        return True
    job["track_status"] = f"🔄 Converting {wi}/{total}: {os.path.basename(src)}"
    if fmt == "mp3":
        cmd = ["ffmpeg", "-y", "-i", src, "-vn",
               "-ar", "44100", "-ac", "2",
               "-c:a", "libmp3lame", "-b:a", "320k", "-q:a", "0",
               out_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", src, "-vn",
               "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", out_path]
    conv = subprocess.run(cmd, capture_output=True, text=True)
    if conv.returncode != 0:
        job["log"].append(f"  ✗ Convert failed track {wi}: {conv.stderr[-200:]}")
        return False
    return True


def process_album(idx, total, album_id, output_dir, tmp_base, job):
    """Download + convert + merge one album. Returns True on success."""
    album = next((a for a in state["albums"] if a["Id"] == album_id), None)
    if not album:
        job["log"].append(f"[{idx}/{total}] ⚠ Album {album_id} not found, skipping")
        return False

    album_name = safe_name(album.get("Name", album_id))
    out_filename = f"{idx:02d}-{album_name}.wav"
    out_path = os.path.join(output_dir, out_filename)

    job["log"].append(f"[{idx}/{total}] 📀 {album_name}")
    job["current"] = f"{idx}/{total}: {album_name}"

    if os.path.exists(out_path):
        job["log"].append(f"  ✓ Already exists, skipping")
        return True

    tracks = get_album_tracks(album_id)
    if not tracks:
        job["log"].append(f"  ⚠ No tracks found")
        return False

    album_tmp = os.path.join(tmp_base, album_id)
    raw_dir = os.path.join(album_tmp, "raw")
    wav_dir = os.path.join(album_tmp, "wavs")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(wav_dir, exist_ok=True)

    track_total = len(tracks)
    job["log"].append(f"  ⬇ Downloading {track_total} tracks ({DL_WORKERS} parallel)…")

    # ── Phase 1: parallel download ──────────────────────────────────────────
    track_files_map = {}

    def dl_one(ti, t, tt):
        fp = download_track(t, raw_dir, job=job, track_num=ti, track_total=tt)
        return ti, fp

    with ThreadPoolExecutor(max_workers=DL_WORKERS) as pool:
        futures = {pool.submit(dl_one, ti, t, track_total): ti
                   for ti, t in enumerate(tracks, 1)}
        for future in as_completed(futures):
            ti = futures[future]
            try:
                ti_ret, fp = future.result()
                track_files_map[ti_ret] = fp
            except Exception as e:
                tname = tracks[ti - 1].get("Name", "?")
                job["log"].append(f"  ✗ Download failed: {tname}: {e}")

    ordered_raw = [track_files_map[i] for i in sorted(track_files_map)]
    if not ordered_raw:
        job["log"].append(f"  ⚠ No tracks downloaded")
        return False

    job["log"].append(f"  ✓ Downloaded {len(ordered_raw)}/{track_total} — converting to WAV ({CPU_CORES} cores)…")
    job["track_status"] = ""

    # ── Phase 2: parallel WAV conversion (all CPU cores) ───────────────────
    wav_results = {}  # wi -> wav_path or None

    def conv_one(wi, src):
        wav_out = os.path.join(wav_dir, f"{wi:03d}.wav")
        ok = convert_track_to_wav(src, wav_out, job, wi, len(ordered_raw))
        return wi, wav_out if ok else None

    with ThreadPoolExecutor(max_workers=CPU_CORES) as pool:
        futures = {pool.submit(conv_one, wi, src): wi
                   for wi, src in enumerate(ordered_raw, 1)}
        done_count = 0
        for future in as_completed(futures):
            wi, wav_path = future.result()
            wav_results[wi] = wav_path
            done_count += 1
            job["track_status"] = f"🔄 Converted {done_count}/{len(ordered_raw)} tracks for: {album_name}"

    job["track_status"] = ""
    wav_files = [wav_results[i] for i in sorted(wav_results) if wav_results[i]]

    if not wav_files:
        job["log"].append(f"  ⚠ No tracks converted")
        return False

    job["log"].append(f"  ✓ Converted {len(wav_files)}/{len(ordered_raw)} tracks")

    # ── Phase 3: concat WAVs → final output ────────────────────────────────
    list_path = os.path.join(album_tmp, "concat.txt")
    with open(list_path, "w") as f:
        for wp in wav_files:
            f.write(f"file '{wp}'\n")

    job["log"].append(f"  🎵 Merging → {out_filename}")
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", list_path, "-c", "copy", out_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        job["log"].append(f"  ✗ Merge failed: {result.stderr[-300:]}")
        return False

    # Probe final duration as a sanity check
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", out_path],
        capture_output=True, text=True
    )
    dur_str = ""
    if probe.returncode == 0:
        try:
            dur = float(json.loads(probe.stdout)["format"]["duration"])
            dur_str = f"  ({int(dur//60)}m {int(dur%60)}s)"
        except Exception:
            pass

    job["log"].append(f"  ✅ Done: {out_filename}{dur_str}")
    return True


def run_export(job_id, selected_ids, output_dir, fmt="wav", auto_ids=None):
    auto_ids = set(auto_ids or [])
    job = state["jobs"][job_id]
    job["status"] = "running"
    job["track_status"] = ""
    job["done_count"] = 0

    os.makedirs(output_dir, exist_ok=True)
    tmp_base = tempfile.mkdtemp(prefix="jf_export_")

    # ── Sort selected albums by release date (oldest first = track 1) ──────
    def album_sort_key(aid):
        a = next((x for x in state["albums"] if x["Id"] == aid), None)
        if not a:
            return "9999-01-01"
        premiere = a.get("PremiereDate")  # ISO 8601, e.g. "1991-09-24T00:00:00Z"
        if premiere:
            return premiere[:10]  # "YYYY-MM-DD" — comparable with year-padded fallback
        year = a.get("ProductionYear")
        return f"{year}-01-01" if year else "9999-01-01"

    selected_ids = sorted(selected_ids, key=album_sort_key)
    total = len(selected_ids)
    job["total"] = total

    job["log"].append(f"🚀 Exporting {total} albums as {fmt.upper()}  |  {DL_WORKERS} dl workers  |  {CPU_CORES} convert cores")
    job["log"].append(f"📁 Output: {output_dir}")
    job["log"].append(f"📅 Sorted oldest → newest by release date")

    # Write playlist immediately — captures exactly what was requested,
    # regardless of whether individual downloads succeed or fail later.
    try:
        playlist = {
            "version": 1,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "server": state.get("server", ""),
            "total": total,
            "albums": []
        }
        for aid in selected_ids:
            a = next((x for x in state["albums"] if x["Id"] == aid), None)
            if a:
                playlist["albums"].append({
                    "id": a["Id"],
                    "name": a.get("Name", ""),
                    "artist": a.get("AlbumArtist") or (a.get("AlbumArtists") or [{}])[0].get("Name", ""),
                    "year": a.get("ProductionYear"),
                    "play_count": a.get("_TrackPlayCount", 0),
                    "added_by": "auto" if a["Id"] in auto_ids else "manual",
                })
        playlist_path = os.path.join(output_dir, "playlist.json")
        with open(playlist_path, "w", encoding="utf-8") as pf:
            json.dump(playlist, pf, indent=2, ensure_ascii=False)
        job["log"].append(f"💾 Playlist written: playlist.json")
    except Exception as e:
        job["log"].append(f"⚠ Could not write playlist: {e}")

    try:
        import queue as _queue

        # ── Pipeline: download album N+1 while converting album N ──────────
        # prefetch_q holds (album_id, raw_files_map_or_None) — full downloads,
        # not just track metadata. maxsize=1 means we keep at most 1 album
        # pre-downloaded in the buffer to avoid excessive disk use.
        prefetch_q = _queue.Queue(maxsize=1)

        def prefetch_downloads(album_ids):
            """Download tracks for each album in background, one album ahead."""
            for aid in album_ids:
                try:
                    tracks = get_album_tracks(aid)
                except Exception:
                    prefetch_q.put((aid, None, None))
                    continue
                if not tracks:
                    prefetch_q.put((aid, [], {}))
                    continue
                album_tmp_dl = os.path.join(tmp_base, aid)
                raw_dir_dl = os.path.join(album_tmp_dl, "raw")
                os.makedirs(raw_dir_dl, exist_ok=True)
                track_total = len(tracks)
                files_map = {}

                def dl(ti, t, tt, rd=raw_dir_dl):
                    fp = download_track(t, rd, job=job, track_num=ti, track_total=tt)
                    return ti, fp

                with ThreadPoolExecutor(max_workers=DL_WORKERS) as pool:
                    futures = {pool.submit(dl, ti, t, track_total): ti
                               for ti, t in enumerate(tracks, 1)}
                    for future in as_completed(futures):
                        ti = futures[future]
                        try:
                            ti_ret, fp = future.result()
                            files_map[ti_ret] = fp
                        except Exception as e:
                            pass  # logged per-album below
                prefetch_q.put((aid, tracks, files_map))

        prefetch_thread = threading.Thread(
            target=prefetch_downloads, args=(selected_ids,), daemon=True
        )
        prefetch_thread.start()

        for idx in range(1, total + 1):
            album_id, tracks_prefetched, track_files_map = prefetch_q.get()
            job["progress"] = round((idx - 1) / total * 100)

            album = next((a for a in state["albums"] if a["Id"] == album_id), None)
            if not album:
                job["log"].append(f"[{idx}/{total}] ⚠ Album {album_id} not found, skipping")
                continue

            album_name = safe_name(album.get("Name", album_id))
            album_artist = safe_name(album.get("AlbumArtist") or
                               (album.get("AlbumArtists") or [{}])[0].get("Name", "") or "Unknown")
            premiere = album.get("PremiereDate", "")
            album_year_val = album.get("ProductionYear") or (premiere[:4] if premiere else None) or "unknown"
            out_filename = f"{idx-1:02d}-{album_artist}-({album_year_val})-{album_name}.{fmt}"
            out_path = os.path.join(output_dir, out_filename)

            job["log"].append(f"[{idx}/{total}] 📀 {album_name}")
            job["current"] = f"{idx}/{total}: {album_name}"

            if os.path.exists(out_path):
                job["log"].append(f"  ✓ Already exists, skipping")
                job["done_count"] = idx
                job["progress"] = round(idx / total * 100)
                shutil.rmtree(os.path.join(tmp_base, album_id), ignore_errors=True)
                continue

            if tracks_prefetched is None:
                job["log"].append(f"  ✗ Could not fetch tracks")
                continue
            if not tracks_prefetched:
                job["log"].append(f"  ⚠ No tracks found")
                continue

            track_total = len(tracks_prefetched)
            ordered_raw = [track_files_map[i] for i in sorted(track_files_map) if track_files_map.get(i)]
            if not ordered_raw:
                job["log"].append(f"  ⚠ No tracks downloaded")
                continue

            job["log"].append(f"  ✓ {len(ordered_raw)}/{track_total} tracks — converting ({CPU_CORES} cores)…")
            job["track_status"] = ""

            album_tmp = os.path.join(tmp_base, album_id)
            ext = "mp3" if fmt == "mp3" else "wav"
            out_dir = os.path.join(album_tmp, "converted")
            os.makedirs(out_dir, exist_ok=True)

            # Phase 2: parallel per-track encode (WAV or MP3)
            conv_results = {}

            def conv_one(wi, src, total_tracks, od=out_dir, f=fmt):
                out_p = os.path.join(od, f"{wi:03d}.{f}")
                ok = convert_track(src, out_p, f, job, wi, total_tracks)
                return wi, out_p if ok else None

            with ThreadPoolExecutor(max_workers=CPU_CORES) as pool:
                futures = {pool.submit(conv_one, wi, src, len(ordered_raw)): wi
                           for wi, src in enumerate(ordered_raw, 1)}
                done_conv = 0
                for future in as_completed(futures):
                    wi, cp = future.result()
                    conv_results[wi] = cp
                    done_conv += 1
                    job["track_status"] = f"🔄 {done_conv}/{len(ordered_raw)} encoded — {album_name}"

            job["track_status"] = ""
            conv_files = [conv_results[i] for i in sorted(conv_results) if conv_results[i]]

            if not conv_files:
                job["log"].append(f"  ⚠ No tracks converted")
                continue

            job["log"].append(f"  ✓ Encoded {len(conv_files)}/{len(ordered_raw)}")

            # Phase 3: merge
            # WAV  → ffmpeg concat -c copy  (lossless, fast)
            # MP3  → binary cat             (MP3 frames concatenate natively, instant)
            job["log"].append(f"  🎵 Merging → {out_filename}")
            if fmt == "mp3":
                # Binary concatenation — no re-encode, no quality loss
                with open(out_path, "wb") as fout:
                    for cp in conv_files:
                        with open(cp, "rb") as fin:
                            shutil.copyfileobj(fin, fout)
                merge_ok = True
            else:
                list_path = os.path.join(album_tmp, "concat.txt")
                with open(list_path, "w") as lf:
                    for wp in conv_files:
                        lf.write(f"file '{wp}'\n")
                result = subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                     "-i", list_path, "-c", "copy", out_path],
                    capture_output=True, text=True
                )
                merge_ok = result.returncode == 0
                if not merge_ok:
                    job["log"].append(f"  ✗ Merge failed: {result.stderr[-300:]}")

            if not merge_ok:
                continue

            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", out_path],
                capture_output=True, text=True
            )
            dur_str = ""
            if probe.returncode == 0:
                try:
                    dur = float(json.loads(probe.stdout)["format"]["duration"])
                    dur_str = f"  ({int(dur//60)}m {int(dur%60)}s)"
                except Exception:
                    pass

            job["log"].append(f"  ✅ Done: {out_filename}{dur_str}")
            job["completed_files"].append(out_filename)
            job["done_count"] = idx

            # Clean up this album's temp files immediately to save disk
            shutil.rmtree(album_tmp, ignore_errors=True)

            job["progress"] = round(idx / total * 100)

    except Exception as e:
        import traceback
        job["log"].append(f"💥 Fatal error: {e}")
        job["log"].append(traceback.format_exc()[-500:])
        job["status"] = "error"
    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)

    if job["status"] != "error":
        job["status"] = "done"
        job["progress"] = 100
        job["log"].append(f"✅ All done! {total} albums exported to: {output_dir}")

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/connect", methods=["POST"])
def api_connect():
    d = request.json
    server = d.get("server", "").strip()
    if not server:
        return jsonify({"ok": False, "error": "Server URL required"})
    try:
        if d.get("api_key"):
            authenticate(server, api_key=d["api_key"])
        else:
            authenticate(server, username=d.get("username"), password=d.get("password"))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/albums")
def api_albums():
    try:
        albums = fetch_all_albums()
        state["albums"] = albums
        simplified = []
        for a in albums:
            simplified.append({
                "id": a["Id"],
                "name": a.get("Name", ""),
                "artist": a.get("AlbumArtist") or (a.get("AlbumArtists") or [{}])[0].get("Name", ""),
                "year": a.get("ProductionYear"),
                "tracks": a.get("ChildCount", 0),
                "duration": ticks_to_duration(a.get("CumulativeRunTimeTicks")),
                "genres": a.get("Genres", []),
                "play_count": a.get("_TrackPlayCount") or a.get("UserData", {}).get("PlayCount", 0),
                "last_played": a.get("UserData", {}).get("LastPlayedDate", ""),
                "image": f"/api/image/{a['Id']}",
            })
        return jsonify({"ok": True, "albums": simplified})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/image/<album_id>")
def api_image(album_id):
    try:
        url = jf_url(f"/Items/{album_id}/Images/Primary")
        r = requests.get(url, headers=jf_headers(), timeout=10, params={"maxWidth": 200})
        if r.status_code == 200:
            from flask import Response
            return Response(r.content, content_type=r.headers.get("Content-Type", "image/jpeg"))
    except:
        pass
    # return tiny gray placeholder SVG
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><rect width="200" height="200" fill="#2a2a2a"/><text x="100" y="110" text-anchor="middle" fill="#555" font-size="48">♪</text></svg>'
    from flask import Response
    return Response(svg, content_type="image/svg+xml")

@app.route("/api/export", methods=["POST"])
def api_export():
    d = request.json
    selected = d.get("selected", [])
    auto_ids  = set(d.get("auto_ids", []))   # which ones were auto-filled
    output_dir = d.get("output_dir", "").strip()
    fmt = d.get("format", "wav")  # "wav" or "mp3"
    if fmt not in ("wav", "mp3"):
        fmt = "wav"
    if not selected:
        return jsonify({"ok": False, "error": "No albums selected"})
    if not output_dir:
        output_dir = os.path.join(Path.home(), "Downloads", "jellyfin-export")

    job_id = f"{int(time.time())}_{id(selected) & 0xffff}"
    state["jobs"][job_id] = {
        "status": "starting",
        "progress": 0,
        "total": len(selected),
        "current": "",
        "output_dir": output_dir,
        "completed_files": [],   # filenames ready to fetch
        "log": [f"🚀 Starting export of {len(selected)} albums to: {output_dir}"],
    }
    t = threading.Thread(target=run_export, args=(job_id, selected, output_dir, fmt, auto_ids), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/api/job/<job_id>")
def api_job(job_id):
    job = state["jobs"].get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"})
    return jsonify({
        "ok": True,
        "status": job["status"],
        "progress": job["progress"],
        "total": job.get("total", 0),
        "current": job.get("current", ""),
        "track_status": job.get("track_status", ""),
        "log": job.get("log", []),
        "completed_files": job.get("completed_files", []),
    })

@app.route("/api/status")
def api_status():
    return jsonify({"connected": state["token"] is not None})

@app.route("/api/fetch-file/<job_id>/<path:filename>")
def api_fetch_file(job_id, filename):
    """Serve a completed export file so the browser can write it to a chosen folder."""
    job = state["jobs"].get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    output_dir = job.get("output_dir", "")
    if not output_dir:
        return jsonify({"error": "No output dir"}), 404
    file_path = os.path.join(output_dir, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404
    from flask import send_file
    mime = "audio/mpeg" if filename.endswith(".mp3") else "audio/wav"
    return send_file(file_path, mimetype=mime, as_attachment=False)

@app.route("/api/resolve-folder", methods=["POST"])
def api_resolve_folder():
    """Try to find a folder/file by name in common locations (Downloads, home, mounts)."""
    d = request.json
    name = d.get("name", "").strip()
    is_file = d.get("is_file", False)
    # Search common locations
    candidates = [
        Path.home() / "Downloads" / name,
        Path.home() / name,
        Path("/media") / os.environ.get("USER", "") / name,
        Path("/mnt") / name,
        Path("/run/media") / os.environ.get("USER", "") / name,
    ]
    for c in candidates:
        if is_file and c.is_file():
            return jsonify({"path": str(c)})
        elif not is_file and c.is_dir():
            return jsonify({"path": str(c)})
    # Can't resolve — let browser handle it
    return jsonify({"path": None})

@app.route("/api/load-playlist-data", methods=["POST"])
def api_load_playlist_data():
    """Load a playlist from JSON data sent directly from the browser."""
    d = request.json
    data_str = d.get("data", "")
    try:
        playlist = json.loads(data_str)
        albums_in_list = playlist.get("albums", [])
        matched = []
        auto_matched = []
        not_found = []
        for entry in albums_in_list:
            found = next((a for a in state["albums"] if a["Id"] == entry["id"]), None)
            if not found:
                ename = entry.get("name", "").lower().strip()
                eartist = entry.get("artist", "").lower().strip()
                found = next((
                    a for a in state["albums"]
                    if a.get("Name","").lower().strip() == ename and
                       (a.get("AlbumArtist","") or "").lower().strip() == eartist
                ), None)
            if found:
                matched.append(found["Id"])
                if entry.get("added_by") == "auto":
                    auto_matched.append(found["Id"])
            else:
                not_found.append(f"{entry.get('artist','')} - {entry.get('name','')}")
        return jsonify({
            "ok": True, "matched": matched, "auto_matched": auto_matched,
            "not_found": not_found,
            "total": len(albums_in_list),
            "server": playlist.get("server", ""),
            "created": playlist.get("created", ""),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/pick-folder")
def api_pick_folder():
    """Open native OS folder picker via tkinter. Returns chosen path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()          # hide the empty root window
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Select output folder")
        root.destroy()
        if path:
            return jsonify({"ok": True, "path": path})
        return jsonify({"ok": False, "cancelled": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/pick-file")
def api_pick_file():
    """Open native OS file picker for playlist.json."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Select playlist.json",
            filetypes=[("Jellyfin Playlist", "playlist*.json"), ("JSON files", "*.json"), ("All files", "*")],
        )
        root.destroy()
        if path:
            return jsonify({"ok": True, "path": path})
        return jsonify({"ok": False, "cancelled": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/load-playlist", methods=["POST"])
def api_load_playlist():
    d = request.json
    playlist_path = d.get("path", "").strip()
    if not playlist_path:
        return jsonify({"ok": False, "error": "No path provided"})
    if not os.path.exists(playlist_path):
        return jsonify({"ok": False, "error": f"File not found: {playlist_path}"})
    try:
        with open(playlist_path, "r", encoding="utf-8") as f:
            playlist = json.load(f)
        albums_in_list = playlist.get("albums", [])
        # Match by ID first, then fall back to name+artist match
        matched = []
        auto_matched = []   # ids that were originally auto-filled
        not_found = []
        for entry in albums_in_list:
            # Try ID match
            found = next((a for a in state["albums"] if a["Id"] == entry["id"]), None)
            if not found:
                # Try name + artist match (server may have different IDs after migration)
                ename = entry.get("name", "").lower().strip()
                eartist = entry.get("artist", "").lower().strip()
                found = next((
                    a for a in state["albums"]
                    if a.get("Name","").lower().strip() == ename and
                       (a.get("AlbumArtist","") or "").lower().strip() == eartist
                ), None)
            if found:
                matched.append(found["Id"])
                if entry.get("added_by") == "auto":
                    auto_matched.append(found["Id"])
            else:
                not_found.append(f"{entry.get('artist','')} - {entry.get('name','')}")
        return jsonify({
            "ok": True,
            "matched": matched,
            "auto_matched": auto_matched,
            "not_found": not_found,
            "total": len(albums_in_list),
            "server": playlist.get("server", ""),
            "created": playlist.get("created", ""),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── HTML / UI ────────────────────────────────────────────────────────────────

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jellyfin Album Picker</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@300;400;500&display=swap');

  :root {
    --bg: #0d0d0d;
    --surface: #141414;
    --surface2: #1c1c1c;
    --border: #2a2a2a;
    --accent: #e8c547;
    --accent2: #c4373a;
    --text: #e8e8e8;
    --muted: #666;
    --green: #4caf50;
    --red: #c4373a;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Mono', monospace;
    font-size: 13px;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    gap: 20px;
    padding: 12px 20px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    flex-shrink: 0;
  }
  header h1 {
    font-family: 'Bebas Neue', sans-serif;
    font-size: 26px;
    letter-spacing: 3px;
    color: var(--accent);
  }
  header .pill {
    font-size: 11px;
    padding: 3px 8px;
    border-radius: 20px;
    background: var(--border);
    color: var(--muted);
  }
  header .pill.connected { background: #1a2e1a; color: var(--green); }
  .header-right { margin-left: auto; display: flex; gap: 10px; align-items: center; }

  /* ── Main layout ── */
  .main {
    display: flex;
    flex: 1;
    overflow: hidden;
  }

  /* ── Sidebar ── */
  .sidebar {
    width: 280px;
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    overflow: hidden;
  }
  .sidebar-section {
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
  }
  .sidebar-section label {
    display: block;
    font-size: 10px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .sidebar-section input {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 7px 10px;
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    margin-bottom: 6px;
    border-radius: 3px;
    outline: none;
    transition: border-color .2s;
  }
  .sidebar-section input:focus { border-color: var(--accent); }
  .sidebar-section input[type="password"] { letter-spacing: 2px; }

  button {
    background: var(--accent);
    color: #000;
    border: none;
    padding: 8px 14px;
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    border-radius: 3px;
    transition: opacity .15s;
    letter-spacing: .5px;
  }
  button:hover { opacity: .85; }
  button:disabled { opacity: .4; cursor: not-allowed; }
  button.secondary {
    background: var(--surface2);
    color: var(--text);
    border: 1px solid var(--border);
  }
  button.danger { background: var(--red); color: #fff; }

  .selected-counter {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .count-display {
    font-family: 'Bebas Neue', sans-serif;
    font-size: 36px;
    color: var(--accent);
    line-height: 1;
  }
  .count-label { font-size: 10px; color: var(--muted); letter-spacing: 1px; }
  .count-over { color: var(--red); }

  /* Selection list in sidebar */
  .selection-list {
    flex: 1;
    overflow-y: auto;
    padding: 8px;
  }
  .sel-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 6px;
    border-radius: 3px;
    background: var(--surface2);
    margin-bottom: 4px;
    font-size: 11px;
    cursor: grab;
  }
  .sel-item:active { cursor: grabbing; }
  .sel-item .sel-num {
    font-family: 'Bebas Neue', sans-serif;
    font-size: 16px;
    color: var(--accent);
    min-width: 26px;
    text-align: center;
  }
  .sel-item .sel-name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sel-item .sel-remove { color: var(--muted); cursor: pointer; font-size: 14px; padding: 2px 4px; }
  .sel-item .sel-remove:hover { color: var(--red); }
  .sel-item.dragging { opacity: .4; }

  /* Export area */
  .export-area {
    padding: 12px 16px;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  /* ── Album grid ── */
  .content {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .toolbar {
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    gap: 10px;
    align-items: center;
    flex-shrink: 0;
    background: var(--surface);
  }
  .toolbar input[type="text"] {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 7px 12px;
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    border-radius: 3px;
    outline: none;
    width: 240px;
    transition: border-color .2s;
  }
  .toolbar input[type="text"]:focus { border-color: var(--accent); }
  .toolbar select {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 7px 10px;
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    border-radius: 3px;
    outline: none;
    cursor: pointer;
  }
  .album-count { margin-left: auto; color: var(--muted); font-size: 11px; }

  .grid-area {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
  }
  .albums-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 12px;
  }

  .album-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
    cursor: pointer;
    transition: border-color .15s, transform .15s;
    position: relative;
    user-select: none;
  }
  .album-card:hover { border-color: var(--accent); transform: translateY(-2px); }
  .album-card.selected { border-color: var(--accent); }
  .album-card.selected::after {
    content: attr(data-num);
    position: absolute;
    top: 6px;
    right: 6px;
    background: var(--accent);
    color: #000;
    font-family: 'Bebas Neue', sans-serif;
    font-size: 18px;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    line-height: 28px;
    text-align: center;
  }
  .album-art {
    width: 100%;
    aspect-ratio: 1;
    object-fit: cover;
    background: var(--surface2);
    display: block;
  }
  .album-info {
    padding: 8px 10px 10px;
  }
  .album-title {
    font-size: 12px;
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 3px;
  }
  .album-artist {
    font-size: 10px;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .album-plays {
    font-size: 10px;
    color: var(--accent);
    margin-top: 4px;
  }
  .album-plays.unplayed { color: var(--muted); }
  .album-meta {
    font-size: 10px;
    color: var(--muted);
    margin-top: 4px;
    display: flex;
    gap: 8px;
  }

  /* ── Modal ── */
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,.8);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
    display: none;
  }
  .modal-backdrop.show { display: flex; }
  .modal {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    width: 680px;
    max-width: 95vw;
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .modal-header {
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .modal-header h2 {
    font-family: 'Bebas Neue', sans-serif;
    font-size: 22px;
    letter-spacing: 2px;
    color: var(--accent);
  }
  .modal-close { color: var(--muted); cursor: pointer; font-size: 20px; line-height: 1; }
  .modal-close:hover { color: var(--text); }
  .modal-body { padding: 20px; overflow-y: auto; }
  .log-box {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 12px;
    font-size: 11px;
    height: 260px;
    overflow-y: auto;
    line-height: 1.7;
  }
  .progress-bar-wrap {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 3px;
    height: 8px;
    margin-bottom: 12px;
    overflow: hidden;
  }
  .progress-bar-fill {
    height: 100%;
    background: var(--accent);
    transition: width .4s;
  }

  /* ── Scrollbar ── */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .tag { font-size: 10px; padding: 2px 6px; background: var(--surface2); border-radius: 2px; color: var(--muted); }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--muted);
    gap: 12px;
    font-size: 13px;
  }
  .empty-state .big { font-family: 'Bebas Neue', sans-serif; font-size: 72px; color: var(--border); line-height: 1; }

  /* ── Auto-selected cards (teal) ── */
  .album-card.auto-selected { border-color: #3ecfcf; }
  .album-card.auto-selected::after {
    background: #3ecfcf;
    color: #000;
  }

  /* ── Genre view ── */
  .genre-section { margin-bottom: 24px; }
  .genre-header {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 4px;
    margin-bottom: 10px;
    cursor: pointer;
    user-select: none;
    background: var(--surface2);
    transition: background .15s;
  }
  .genre-header:hover { background: #222; }
  .genre-header h3 {
    font-family: 'Bebas Neue', sans-serif;
    font-size: 22px;
    letter-spacing: 2px;
    color: var(--accent);
  }
  .genre-header .genre-count {
    font-size: 10px;
    color: var(--muted);
    background: var(--bg);
    padding: 2px 7px;
    border-radius: 10px;
  }
  .genre-header .genre-chevron {
    margin-left: auto;
    color: var(--accent);
    font-size: 18px;
    font-weight: bold;
    transition: transform .2s;
    min-width: 24px;
    text-align: center;
  }
  .genre-header.collapsed .genre-chevron { transform: rotate(-90deg); }
  .genre-body.hidden { display: none; }

  /* ── Sidebar auto section ── */
  .auto-section-header {
    padding: 6px 8px 4px;
    font-size: 10px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: #3ecfcf;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .sel-item.auto { background: #0e2424; border-left: 2px solid #3ecfcf; }
  .sel-item.auto .sel-num { color: #3ecfcf; }

  /* ── View toggle button ── */
  .view-toggle {
    display: flex;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 3px;
    overflow: hidden;
  }
  .view-toggle button {
    background: transparent;
    color: var(--muted);
    border: none;
    padding: 6px 10px;
    font-size: 11px;
    border-radius: 0;
  }
  .view-toggle button.active {
    background: var(--surface2);
    color: var(--text);
  }

  /* Recent folders */
  #recents-list { font-size: 10px; }
  .recent-item {
    padding: 4px 8px;
    border-radius: 3px;
    cursor: pointer;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    transition: background .1s, color .1s;
  }
  .recent-item:hover { background: var(--surface2); color: var(--text); }
</style>
</head>
<body>

<header>
  <h1>🎵 Jellyfin Picker</h1>
  <span class="pill" id="conn-pill">Not connected</span>
  <div class="header-right">
    <span id="album-total-header" style="color:var(--muted);font-size:11px;"></span>
  </div>
</header>

<div class="main">

  <!-- Sidebar -->
  <div class="sidebar">
    <!-- Connect form -->
    <div class="sidebar-section" id="connect-section">
      <label>Jellyfin Server</label>
      <input id="inp-server" type="text" placeholder="http://192.168.1.x:8096" />
      <label>Username</label>
      <input id="inp-user" type="text" placeholder="admin" />
      <label>Password</label>
      <input id="inp-pass" type="password" placeholder="••••••••" />
      <div style="font-size:10px;color:var(--muted);margin:4px 0 6px;">or use API key instead:</div>
      <input id="inp-apikey" type="text" placeholder="API Key (optional)" />
      <button onclick="connect()" style="width:100%;margin-top:4px;" id="btn-connect">Connect & Load Albums</button>
    </div>

    <!-- Selection counter -->
    <div class="selected-counter">
      <div>
        <div class="count-display" id="sel-count">0</div>
        <div class="count-label" id="sel-label">/ 100 SELECTED</div>
      </div>
      <button onclick="autoFill()" id="btn-auto" style="font-size:11px;background:#3ecfcf;color:#000;padding:6px 10px;">✦ Auto-fill</button>
    </div>

    <!-- Manual picks header -->
    <div style="padding:5px 8px 3px;font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border);">
      <span>MY PICKS (<span id="manual-count">0</span>)</span>
      <button class="secondary" onclick="clearManual()" style="font-size:10px;padding:2px 7px;">✕ Clear</button>
    </div>

    <!-- Manual picks -->
    <div class="selection-list" id="selection-list" style="flex:none;max-height:36%;overflow-y:auto;padding:8px;border-bottom:1px solid var(--border);"></div>

    <!-- Auto-selected section -->
    <div style="flex:1;overflow-y:auto;display:flex;flex-direction:column;">
      <div class="auto-section-header" id="auto-header" style="display:none;">
        <span>✦ AUTO (<span id="auto-count">0</span>)</span>
        <div style="display:flex;gap:4px;">
          <button onclick="reRandomize()" style="font-size:10px;padding:3px 7px;background:#3ecfcf;color:#000;">↺ Redo</button>
          <button onclick="clearAuto()" style="font-size:10px;padding:3px 7px;background:var(--surface2);color:var(--text);border:1px solid var(--border);">✕ Clear</button>
        </div>
      </div>
      <div style="flex:1;overflow-y:auto;padding:0 8px 8px;" id="auto-list"></div>
    </div>

    <!-- Export -->
    <div class="export-area">
      <!-- Output folder row -->
      <div style="display:flex;gap:6px;margin-bottom:4px;">
        <input id="inp-outdir" type="text" placeholder="~/Downloads/jellyfin-export (default)"
          style="flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);
                 padding:7px 10px;font-family:'DM Mono',monospace;font-size:11px;
                 border-radius:3px;outline:none;min-width:0;"
          id="inp-outdir" />
        <button onclick="pickFolder()" class="secondary" style="flex:0 0 auto;padding:7px 11px;font-size:15px;line-height:1;" title="Browse">📁</button>
      </div>
      <!-- Recent folders -->
      <div id="recents-list" style="display:none;margin-bottom:6px;"></div>
      <!-- Format toggle -->
      <div style="display:flex;gap:6px;margin-bottom:6px;">
        <div class="view-toggle" style="flex:1;">
          <button id="fmt-wav" onclick="setFmt('wav')" style="flex:1;width:50%;">WAV</button>
          <button id="fmt-mp3" class="active" onclick="setFmt('mp3')" style="flex:1;width:50%;">MP3 320k</button>
        </div>
      </div>
      <div style="display:flex;gap:6px;">
        <button onclick="startExport()" id="btn-export" style="flex:1;">Export MP3 →</button>
        <button onclick="openImportModal()" class="secondary" style="flex:0 0 auto;padding:8px 10px;" title="Import playlist.json">⬆ Import</button>
      </div>
    </div>
  </div>

  <!-- Main content -->
  <div class="content">
    <div class="toolbar">
      <input type="text" id="search-box" placeholder="Search albums or artists…" oninput="filterAlbums()">
      <select id="sort-by" onchange="filterAlbums()">
        <option value="name">Sort: Name</option>
        <option value="artist">Sort: Artist</option>
        <option value="year">Sort: Year</option>
        <option value="tracks">Sort: Tracks</option>
        <option value="play_count">Sort: Play Count</option>
        <option value="last_played">Sort: Last Played</option>
      </select>
      <select id="sort-dir" onchange="filterAlbums()">
        <option value="asc">↑ Asc</option>
        <option value="desc">↓ Desc</option>
      </select>
      <div class="view-toggle">
        <button id="btn-view-grid" class="active" onclick="setView('grid')">⊞ Grid</button>
        <button id="btn-view-genre" onclick="setView('genre')">♬ Genre</button>
      </div>
      <div id="genre-controls" style="display:none;gap:6px;display:none;">
        <button class="secondary" onclick="collapseAllGenres()" style="font-size:11px;padding:5px 10px;">⊟ Collapse all</button>
        <button class="secondary" onclick="expandAllGenres()" style="font-size:11px;padding:5px 10px;">⊞ Expand all</button>
      </div>
      <span class="album-count" id="shown-count"></span>
    </div>
    <div class="grid-area">
      <div id="grid-content">
        <div class="empty-state">
          <div class="big">♪</div>
          <div>Connect to your Jellyfin server to begin</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Import playlist modal -->
<div class="modal-backdrop" id="import-modal">
  <div class="modal" style="width:500px;">
    <div class="modal-header">
      <h2>Import Playlist</h2>
      <span class="modal-close" onclick="closeImportModal()">✕</span>
    </div>
    <div class="modal-body">
      <div style="font-size:11px;color:var(--muted);margin-bottom:14px;">
        Select a <code style="color:var(--accent)">playlist.json</code> from a previous export.
        Albums matched by ID, then name+artist as fallback.
      </div>
      <!-- Hidden native file input — triggers real OS file manager -->
      <input type="file" id="playlist-file-input" accept=".json,application/json"
        style="display:none;" onchange="onPlaylistFileChosen(this)" />
      <!-- File picker row -->
      <div style="display:flex;gap:6px;margin-bottom:10px;">
        <input id="inp-playlist-path" type="text" placeholder="Click 📂 to browse for playlist.json"
          style="flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:8px 10px;font-family:'DM Mono',monospace;font-size:12px;border-radius:3px;outline:none;min-width:0;" readonly />
        <button onclick="pickPlaylistFile()" class="secondary" style="flex:0 0 auto;padding:8px 12px;font-size:16px;line-height:1;" title="Browse for file">📂</button>
      </div>
      <div style="display:flex;gap:8px;margin-bottom:12px;">
        <button onclick="loadPlaylist('replace')" style="flex:1;">↺ Replace selection</button>
        <button onclick="loadPlaylist('merge')" class="secondary" style="flex:1;">⊕ Merge with current</button>
      </div>
      <div id="import-result" style="font-size:11px;min-height:40px;"></div>
    </div>
  </div>
</div>

<!-- Export modal -->
<div class="modal-backdrop" id="export-modal">
  <div class="modal">
    <div class="modal-header">
      <h2>Exporting Albums</h2>
      <span class="modal-close" onclick="closeModal()">✕</span>
    </div>
    <div class="modal-body">
      <!-- Album progress bar -->
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-bottom:4px;">
        <span>ALBUM PROGRESS</span>
        <span id="prog-pct">0%</span>
      </div>
      <div class="progress-bar-wrap" style="height:10px;margin-bottom:8px;">
        <div class="progress-bar-fill" id="prog-fill" style="width:0%"></div>
      </div>

      <!-- ETA row -->
      <div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:10px;">
        <span id="eta-elapsed" style="color:var(--muted);">Elapsed: —</span>
        <span id="eta-remaining" style="color:#7ec8c8;">ETA: —</span>
      </div>

      <!-- Current album -->
      <div id="current-album" style="font-size:12px;color:var(--accent);margin-bottom:6px;min-height:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"></div>

      <!-- Live track download status (updates every poll) -->
      <div id="track-status-box" style="font-size:11px;color:#7ec8c8;background:var(--bg);border:1px solid var(--border);border-radius:3px;padding:8px 10px;margin-bottom:10px;min-height:32px;font-family:'DM Mono',monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
        Waiting to start…
      </div>

      <!-- Log history -->
      <div style="font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">Log</div>
      <div class="log-box" id="log-box"></div>
    </div>
  </div>
</div>

<script>
let allAlbums = [];
let selected = [];     // manually picked album ids (ordered)
let autoSelected = []; // auto-filled ids (separate, teal)
let currentView = 'grid'; // 'grid' | 'genre'
let exportFmt = 'mp3';    // 'wav' | 'mp3'
let currentJobId = null;
let pollTimer = null;
let dragSrcIdx = null;
let dragSrcList = null; // 'manual' | 'auto'

function setFmt(f) {
  exportFmt = f;
  document.getElementById('fmt-wav').className = f === 'wav' ? 'active' : '';
  document.getElementById('fmt-mp3').className = f === 'mp3' ? 'active' : '';
  document.getElementById('btn-export').textContent = f === 'mp3' ? 'Export MP3 →' : 'Export WAV →';
}

// ── View toggle ───────────────────────────────────────────────────────────────
// Render recent folders on load
renderRecents();

function setView(v) {
  currentView = v;
  document.getElementById('btn-view-grid').className = v === 'grid' ? 'active' : '';
  document.getElementById('btn-view-genre').className = v === 'genre' ? 'active' : '';
  document.getElementById('sort-by').style.display = v === 'genre' ? 'none' : '';
  document.getElementById('sort-dir').style.display = v === 'genre' ? 'none' : '';
  document.getElementById('genre-controls').style.display = v === 'genre' ? 'flex' : 'none';
  filterAlbums();
}

function collapseAllGenres() {
  document.querySelectorAll('.genre-header').forEach(h => {
    h.classList.add('collapsed');
    h.nextElementSibling.classList.add('hidden');
  });
}

function expandAllGenres() {
  document.querySelectorAll('.genre-header').forEach(h => {
    h.classList.remove('collapsed');
    h.nextElementSibling.classList.remove('hidden');
  });
}

// ── Connect ──────────────────────────────────────────────────────────────────
async function connect() {
  const server = document.getElementById('inp-server').value.trim();
  const username = document.getElementById('inp-user').value.trim();
  const password = document.getElementById('inp-pass').value;
  const api_key = document.getElementById('inp-apikey').value.trim();
  if (!server) return alert('Enter server URL');

  const btn = document.getElementById('btn-connect');
  btn.disabled = true;
  btn.textContent = 'Connecting…';

  const r = await fetch('/api/connect', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({server, username, password, api_key})
  });
  const d = await r.json();
  if (!d.ok) {
    alert('Connection failed: ' + d.error);
    btn.disabled = false;
    btn.textContent = 'Connect & Load Albums';
    return;
  }

  const pill = document.getElementById('conn-pill');
  const shortServer = document.getElementById('inp-server').value.replace(/https?:\/\//, '');
  pill.textContent = '● ' + shortServer;
  pill.className = 'pill connected';
  btn.textContent = 'Loading albums…';

  await loadAlbums();
  btn.disabled = false;
  btn.textContent = 'Reload Albums';
}

async function loadAlbums() {
  const gc = document.getElementById('grid-content');
  gc.innerHTML = '<div class="empty-state"><div class="big">…</div><div>Loading albums + play counts…</div></div>';
  const r = await fetch('/api/albums');
  const d = await r.json();
  if (!d.ok) { alert('Failed to load: ' + d.error); return; }
  allAlbums = d.albums;
  document.getElementById('album-total-header').textContent = allAlbums.length + ' albums';
  document.title = `Jellyfin Picker — ${allAlbums.length} albums`;
  filterAlbums();
}

// ── Filter & render grid ─────────────────────────────────────────────────────
function filterAlbums() {
  const q = document.getElementById('search-box').value.toLowerCase();
  const sortByEl = document.getElementById('sort-by');
  const sortDirEl = document.getElementById('sort-dir');
  if ((sortByEl.value === 'play_count' || sortByEl.value === 'last_played') && event && event.type === 'change' && event.target === sortByEl) {
    sortDirEl.value = 'desc';
  }
  const sortBy = sortByEl.value;
  const sortDir = sortDirEl.value;

  let filtered = allAlbums.filter(a =>
    a.name.toLowerCase().includes(q) ||
    (a.artist||'').toLowerCase().includes(q) ||
    (currentView === 'genre' && (a.genres||[]).some(g => g.toLowerCase().includes(q)))
  );

  if (currentView === 'genre') {
    document.getElementById('shown-count').textContent = `${filtered.length} of ${allAlbums.length}`;
    renderGenreView(filtered);
    return;
  }

  filtered.sort((a, b) => {
    let av = a[sortBy] ?? '', bv = b[sortBy] ?? '';
    if (typeof av === 'string') av = av.toLowerCase();
    if (typeof bv === 'string') bv = bv.toLowerCase();
    if (typeof av === 'number' && typeof bv === 'number') return av - bv;
    return av < bv ? -1 : av > bv ? 1 : 0;
  });
  if (sortDir === 'desc') filtered.reverse();

  document.getElementById('shown-count').textContent = `${filtered.length} of ${allAlbums.length}`;
  renderGrid(filtered);
}

function makeCard(a) {
  const manualIdx = selected.indexOf(a.id);
  const autoIdx = autoSelected.indexOf(a.id);
  const isManual = manualIdx !== -1;
  const isAuto = autoIdx !== -1;
  const card = document.createElement('div');
  let cls = 'album-card';
  if (isManual) cls += ' selected';
  else if (isAuto) cls += ' auto-selected';
  card.className = cls;
  // badge number = combined position
  const combinedIdx = isManual ? manualIdx : (isAuto ? selected.length + autoIdx : -1);
  card.dataset.num = combinedIdx >= 0 ? combinedIdx : '';  // 0-based, matches filename
  card.dataset.id = a.id;
  card.innerHTML = `
    <img class="album-art" src="${a.image}" loading="lazy" onerror="this.src=''" />
    <div class="album-info">
      <div class="album-title" title="${esc(a.name)}">${esc(a.name)}</div>
      <div class="album-artist" title="${esc(a.artist||'')}">${esc(a.artist||'Unknown')}</div>
      <div class="album-meta">
        <span>${a.year||'—'}</span>
        <span>${a.tracks} trk</span>
        <span>${a.duration}</span>
      </div>
      ${a.play_count > 0 ? `<div class="album-plays">▶ ${a.play_count} play${a.play_count !== 1 ? 's' : ''}</div>` : '<div class="album-plays unplayed">◌ unplayed</div>'}
    </div>`;
  card.onclick = () => toggleAlbum(a.id);
  return card;
}

function renderGrid(albums) {
  const gc = document.getElementById('grid-content');
  if (!albums.length) {
    gc.innerHTML = '<div class="empty-state"><div class="big">∅</div><div>No albums match</div></div>';
    return;
  }
  const grid = document.createElement('div');
  grid.className = 'albums-grid';
  albums.forEach(a => grid.appendChild(makeCard(a)));
  gc.innerHTML = '';
  gc.appendChild(grid);
}

function renderGenreView(albums) {
  const gc = document.getElementById('grid-content');
  // Group albums by genre; albums with no genre go under "Other"
  const genreMap = {};
  albums.forEach(a => {
    const genres = (a.genres && a.genres.length) ? a.genres : ['Other'];
    genres.forEach(g => {
      if (!genreMap[g]) genreMap[g] = [];
      // avoid duplicates (album in multiple genres)
      if (!genreMap[g].find(x => x.id === a.id)) genreMap[g].push(a);
    });
  });
  // Sort genres by total play count of their albums (descending), then alpha
  const sorted = Object.keys(genreMap).sort((a, b) => {
    const playsA = genreMap[a].reduce((s, x) => s + (x.play_count || 0), 0);
    const playsB = genreMap[b].reduce((s, x) => s + (x.play_count || 0), 0);
    if (playsB !== playsA) return playsB - playsA;
    return a.localeCompare(b);
  });
  // Always push "Other" to end
  const otherIdx = sorted.indexOf('Other');
  if (otherIdx > -1) { sorted.splice(otherIdx, 1); sorted.push('Other'); }

  gc.innerHTML = '';
  sorted.forEach(genre => {
    const genreAlbums = genreMap[genre];
    const section = document.createElement('div');
    section.className = 'genre-section';

    const header = document.createElement('div');
    header.className = 'genre-header';
    const genrePlays = genreAlbums.reduce((s, a) => s + (a.play_count || 0), 0);
    header.innerHTML = `
      <h3>${esc(genre)}</h3>
      <span class="genre-count">${genreAlbums.length} album${genreAlbums.length !== 1 ? 's' : ''}</span>
      ${genrePlays > 0 ? `<span class="genre-count" style="color:var(--accent)">▶ ${genrePlays}</span>` : ''}
      <span class="genre-chevron">▼</span>`;
    const body = document.createElement('div');
    body.className = 'albums-grid genre-body';
    genreAlbums.forEach(a => body.appendChild(makeCard(a)));

    header.onclick = () => {
      header.classList.toggle('collapsed');
      body.classList.toggle('hidden');
    };

    section.appendChild(header);
    section.appendChild(body);
    gc.appendChild(section);
  });
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Selection ────────────────────────────────────────────────────────────────
function totalCount() { return selected.length + autoSelected.length; }

function toggleAlbum(id) {
  const manIdx = selected.indexOf(id);
  const autoIdx = autoSelected.indexOf(id);

  if (manIdx !== -1) {
    // deselect manual
    selected.splice(manIdx, 1);
  } else if (autoIdx !== -1) {
    // deselect auto
    autoSelected.splice(autoIdx, 1);
  } else {
    // add as manual pick — no hard cap, counter goes red above 100
    selected.push(id);
  }
  refreshAllCards();
  updateSelectionUI();
}

function refreshAllCards() {
  document.querySelectorAll('.album-card').forEach(card => {
    const id = card.dataset.id;
    const manIdx = selected.indexOf(id);
    const autoIdx = autoSelected.indexOf(id);
    if (manIdx !== -1) {
      card.className = 'album-card selected';
      card.dataset.num = manIdx + 1;
    } else if (autoIdx !== -1) {
      card.className = 'album-card auto-selected';
      card.dataset.num = selected.length + autoIdx + 1;
    } else {
      card.className = 'album-card';
      card.dataset.num = '';
    }
  });
}

function clearSelection() {
  selected = [];
  autoSelected = [];
  refreshAllCards();
  updateSelectionUI();
}

function clearManual() {
  selected = [];
  refreshAllCards();
  updateSelectionUI();
}

function clearAuto() {
  autoSelected = [];
  refreshAllCards();
  updateSelectionUI();
}

function removeManual(id) {
  selected.splice(selected.indexOf(id), 1);
  refreshAllCards();
  updateSelectionUI();
}

function removeAuto(id) {
  autoSelected.splice(autoSelected.indexOf(id), 1);
  refreshAllCards();
  updateSelectionUI();
}

// ── Auto-select ───────────────────────────────────────────────────────────────
function weightedSample(arr, n, weightFn) {
  const out = [];
  const remaining = [...arr];
  for (let i = 0; i < n && remaining.length; i++) {
    const weights = remaining.map(weightFn);
    const total = weights.reduce((s, w) => s + w, 0);
    let r = Math.random() * total;
    let idx = 0;
    for (; idx < weights.length - 1; idx++) {
      r -= weights[idx];
      if (r <= 0) break;
    }
    out.push(remaining.splice(idx, 1)[0]);
  }
  return out;
}

function autoFill() {
  // "Fill" — keeps existing autoSelected, only adds more to reach 100
  const needed = 100 - selected.length - autoSelected.length;
  if (needed <= 0) {
    // already at or over 100 — just re-randomize what we have
    reRandomize(); return;
  }
  const allSelected = new Set([...selected, ...autoSelected]);
  const pool = allAlbums.filter(a => !allSelected.has(a.id));
  if (!pool.length) { alert('No more albums to pick from!'); return; }

  const unplayed = pool.filter(a => a.play_count === 0);
  const played   = pool.filter(a => a.play_count > 0);
  const unplayedSlots = Math.min(Math.round(needed * 0.2), unplayed.length);
  const playedSlots   = Math.min(needed - unplayedSlots, played.length);

  const pickedPlayed   = weightedSample(played,   playedSlots,   a => Math.sqrt(a.play_count) + 1);
  const pickedUnplayed = weightedSample(unplayed, unplayedSlots, () => 1);
  const newOnes = [...pickedPlayed, ...pickedUnplayed].sort(() => Math.random() - 0.5);

  autoSelected = [...autoSelected, ...newOnes.map(a => a.id)];
  refreshAllCards();
  updateSelectionUI();
}

function reRandomize() {
  // Replace all auto picks with a fresh random set
  autoSelected = [];
  const needed = 100 - selected.length;
  if (needed <= 0) return;
  const pool = allAlbums.filter(a => !selected.includes(a.id));
  if (!pool.length) return;

  const unplayed = pool.filter(a => a.play_count === 0);
  const played   = pool.filter(a => a.play_count > 0);
  const unplayedSlots = Math.min(Math.round(needed * 0.2), unplayed.length);
  const playedSlots   = Math.min(needed - unplayedSlots, played.length);

  const pickedPlayed   = weightedSample(played,   playedSlots,   a => Math.sqrt(a.play_count) + 1);
  const pickedUnplayed = weightedSample(unplayed, unplayedSlots, () => 1);
  autoSelected = [...pickedPlayed, ...pickedUnplayed].sort(() => Math.random() - 0.5).map(a => a.id);
  refreshAllCards();
  updateSelectionUI();
}

function updateSelectionUI() {
  const total = totalCount();
  const cnt = document.getElementById('sel-count');
  cnt.textContent = total;
  cnt.className = 'count-display' + (total > 100 ? ' count-over' : '');
  document.getElementById('manual-count').textContent = selected.length;

  // Manual list
  const list = document.getElementById('selection-list');
  list.innerHTML = '';
  selected.forEach((id, i) => {
    const a = allAlbums.find(x => x.id === id);
    if (!a) return;
    const el = document.createElement('div');
    el.className = 'sel-item';
    el.draggable = true;
    el.dataset.idx = i;
    el.innerHTML = `
      <span class="sel-num">${i}</span>
      <span class="sel-name" title="${esc(a.name)}">${esc(a.name)}</span>
      <span class="sel-remove" onclick="removeManual('${id}')">✕</span>`;
    el.addEventListener('dragstart', e => { dragSrcIdx = i; dragSrcList = 'manual'; el.classList.add('dragging'); });
    el.addEventListener('dragend', () => el.classList.remove('dragging'));
    el.addEventListener('dragover', e => e.preventDefault());
    el.addEventListener('drop', e => {
      e.preventDefault();
      if (dragSrcList !== 'manual' || dragSrcIdx === null || dragSrcIdx === i) return;
      const moved = selected.splice(dragSrcIdx, 1)[0];
      selected.splice(i, 0, moved);
      dragSrcIdx = null;
      refreshAllCards();
      updateSelectionUI();
    });
    list.appendChild(el);
  });

  // Auto list
  const autoHeader = document.getElementById('auto-header');
  const autoList = document.getElementById('auto-list');
  const autoCountEl = document.getElementById('auto-count');
  if (autoSelected.length > 0) {
    autoHeader.style.display = 'flex';
    autoCountEl.textContent = autoSelected.length;
    autoList.innerHTML = '';
    autoSelected.forEach((id, i) => {
      const a = allAlbums.find(x => x.id === id);
      if (!a) return;
      const globalNum = selected.length + i;  // 0-based
      const el = document.createElement('div');
      el.className = 'sel-item auto';
      el.innerHTML = `
        <span class="sel-num">${globalNum}</span>
        <span class="sel-name" title="${esc(a.name)}">${esc(a.name)}</span>
        <span style="font-size:9px;color:#3ecfcf;margin-right:2px;">${a.play_count > 0 ? '▶' : '◌'}</span>
        <span class="sel-remove" onclick="removeAuto('${id}')">✕</span>`;
      autoList.appendChild(el);
    });
  } else {
    autoHeader.style.display = 'none';
    autoList.innerHTML = '';
  }
}

// ── Export ───────────────────────────────────────────────────────────────────
let exportStartTime = null;
let etaLastPct = 0;
let etaLastTime = null;
let etaSmoothedSecsPerPct = null;  // EWMA of seconds-per-percent-point
const ETA_ALPHA = 0.25;            // smoothing factor (lower = smoother)

async function startExport() {
  if (selected.length + autoSelected.length === 0) { alert('Select at least one album'); return; }
  const outdir = document.getElementById('inp-outdir').value.trim();

  const modal = document.getElementById('export-modal');
  modal.classList.add('show');
  document.getElementById('log-box').innerHTML = '';
  document.getElementById('log-box')._lineCount = 0;
  document.getElementById('prog-fill').style.width = '0%';
  document.getElementById('prog-pct').textContent = '0%';
  document.getElementById('current-album').textContent = '';
  document.getElementById('track-status-box').textContent = 'Starting…';
  document.getElementById('eta-elapsed').textContent = 'Elapsed: —';
  document.getElementById('eta-remaining').textContent = 'ETA: —';
  exportStartTime = Date.now();
  etaLastPct = 0;
  etaLastTime = Date.now();
  etaSmoothedSecsPerPct = null;

  const allIds = [...selected, ...autoSelected];
  const r = await fetch('/api/export', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      selected: allIds,
      auto_ids: autoSelected,   // so backend can tag each album in playlist.json
      output_dir: outdir,
      format: exportFmt
    })
  });
  const d = await r.json();
  if (!d.ok) { alert('Export failed: ' + d.error); return; }
  currentJobId = d.job_id;
  pollJob();
}

function fmtDuration(seconds) {
  if (!isFinite(seconds) || seconds < 0) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function pollJob() {
  if (pollTimer) clearTimeout(pollTimer);
  fetch('/api/job/' + currentJobId)
    .then(r => r.json())
    .then(d => {
      if (!d.ok) return;

      // album progress bar
      const pct = d.progress || 0;
      document.getElementById('prog-fill').style.width = pct + '%';
      document.getElementById('prog-pct').textContent = pct + '%';

      // ETA: EWMA smoothed — measures how long each % point actually took
      if (exportStartTime) {
        const now = Date.now();
        const elapsed = (now - exportStartTime) / 1000;
        document.getElementById('eta-elapsed').textContent = 'Elapsed: ' + fmtDuration(elapsed);

        if (pct >= 100) {
          document.getElementById('eta-remaining').textContent = 'Done ✓';
          etaSmoothedSecsPerPct = null;
        } else if (pct > etaLastPct && etaLastTime) {
          // A new percent point was crossed — measure how long it took
          const deltaSecs = (now - etaLastTime) / 1000;
          const deltaPct  = pct - etaLastPct;
          const sampledRate = deltaSecs / deltaPct;  // secs per 1%

          if (etaSmoothedSecsPerPct === null) {
            etaSmoothedSecsPerPct = sampledRate;
          } else {
            // exponential weighted moving average — recent samples count more
            etaSmoothedSecsPerPct = ETA_ALPHA * sampledRate + (1 - ETA_ALPHA) * etaSmoothedSecsPerPct;
          }

          etaLastPct  = pct;
          etaLastTime = now;
        }

        if (etaSmoothedSecsPerPct !== null && pct < 100) {
          const remaining = etaSmoothedSecsPerPct * (100 - pct);
          document.getElementById('eta-remaining').textContent = 'ETA: ~' + fmtDuration(remaining);
        } else if (pct < 5) {
          document.getElementById('eta-remaining').textContent = 'ETA: calculating…';
        }
      }

      // current album label
      const albumLabel = d.current
        ? d.current
        : (d.status === 'done' ? '✅ All done!' : d.status);
      document.getElementById('current-album').textContent = albumLabel;

      // On completion, save output dir to recents
      if (d.status === 'done') {
        const dir = document.getElementById('inp-outdir').value.trim();
        if (dir) setOutputDir(dir);
      }

      // live track download ticker
      const tsBox = document.getElementById('track-status-box');
      if (d.track_status) {
        tsBox.textContent = d.track_status;
        tsBox.style.color = '#7ec8c8';
      } else if (d.status === 'done') {
        tsBox.textContent = '✅ Export complete!';
        tsBox.style.color = 'var(--green)';
      } else if (d.status === 'running') {
        tsBox.textContent = '⏳ Processing…';
        tsBox.style.color = 'var(--muted)';
      }

      // log history — only re-render when lines change
      const lb = document.getElementById('log-box');
      const lines = d.log || [];
      if (lines.length !== (lb._lineCount || 0)) {
        lb._lineCount = lines.length;
        // colour-code lines
        lb.innerHTML = lines.map(l => {
          let cls = '';
          if (l.includes('✓')) cls = 'style="color:#4caf50"';
          else if (l.includes('✗') || l.includes('⚠')) cls = 'style="color:var(--red)"';
          else if (l.includes('⬇')) cls = 'style="color:#7ec8c8"';
          else if (l.includes('📀')) cls = 'style="color:var(--accent)"';
          else if (l.includes('🎵')) cls = 'style="color:#b39ddb"';
          else if (l.includes('✅')) cls = 'style="color:var(--green)"';
          return `<div ${cls}>${esc(l)}</div>`;
        }).join('');
        lb.scrollTop = lb.scrollHeight;
      }

      if (d.status === 'running' || d.status === 'starting') {
        pollTimer = setTimeout(pollJob, 400);
      }
    })
    .catch(() => {
      // network blip — retry
      pollTimer = setTimeout(pollJob, 1000);
    });
}

function closeModal() {
  const job = currentJobId;
  if (job) {
    // Only warn if export is actively running
    fetch('/api/job/' + job).then(r => r.json()).then(d => {
      if (d.status === 'running' || d.status === 'starting') {
        if (!confirm('Export is still running. Close anyway? (It will continue in the background)')) return;
      }
      document.getElementById('export-modal').classList.remove('show');
      if (pollTimer) clearTimeout(pollTimer);
    }).catch(() => {
      document.getElementById('export-modal').classList.remove('show');
    });
  } else {
    document.getElementById('export-modal').classList.remove('show');
    if (pollTimer) clearTimeout(pollTimer);
  }
}

// ── Import playlist ───────────────────────────────────────────────────────────
function openImportModal() {
  document.getElementById('import-result').innerHTML = '';
  document.getElementById('import-modal').classList.add('show');
}

function closeImportModal() {
  document.getElementById('import-modal').classList.remove('show');
}

// ── File/folder pickers ──────────────────────────────────────────────────────
// Output folder: must be a real server-side path → use tkinter (runs on server)
// Playlist import: read file in browser → hidden <input type="file">

async function pickFolder() {
  try {
    const r = await fetch('/api/pick-folder');
    const d = await r.json();
    if (d.ok) setOutputDir(d.path);
    // cancelled = do nothing
  } catch(e) {
    alert('Folder picker failed: ' + e);
  }
}

function setOutputDir(path) {
  document.getElementById('inp-outdir').value = path;
  document.getElementById('outdir-label').textContent = path;
  document.getElementById('outdir-label').style.color = 'var(--text)';
  // Save to recents (max 6)
  const recents = getRecents();
  const filtered = recents.filter(p => p !== path);
  filtered.unshift(path);
  saveRecents(filtered.slice(0, 6));
  renderRecents();
}

function getRecents() {
  try { return JSON.parse(localStorage.getItem('jf_recent_dirs') || '[]'); } catch { return []; }
}
function saveRecents(arr) {
  try { localStorage.setItem('jf_recent_dirs', JSON.stringify(arr)); } catch {}
}

function renderRecents() {
  const recents = getRecents();
  const el = document.getElementById('recents-list');
  if (!recents.length) { el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.innerHTML = recents.map(p => {
    const name = p.split(/[\/]/).filter(Boolean).pop() || p;
    return `<div class="recent-item" onclick="setOutputDir(${JSON.stringify(p)})" title="${esc(p)}">
      <span style="color:var(--accent);margin-right:6px;">📁</span>${esc(name)}
    </div>`;
  }).join('');
}

function pickPlaylistFile() {
  // Hidden <input type="file"> — opens native OS file manager directly,
  // reads the file in the browser, sends contents to backend. No path needed.
  document.getElementById('playlist-file-input').click();
}

function onPlaylistFileChosen(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    window._pendingPlaylistData = e.target.result;
    document.getElementById('inp-playlist-path').value = '__browser_loaded__';
    document.getElementById('inp-playlist-path').placeholder = `✓ ${file.name}`;
    // Clear the display value and show filename as placeholder
    document.getElementById('inp-playlist-path').value = '';
    document.getElementById('inp-playlist-path').placeholder = `✓ ${file.name} — ready to import`;
    window._pendingPlaylistData = e.target.result;
    // Auto-trigger visual feedback
    document.getElementById('import-result').innerHTML =
      `<span style="color:var(--accent)">✓ ${esc(file.name)} loaded — choose Replace or Merge</span>`;
  };
  reader.readAsText(file);
  // Reset so same file can be re-picked
  input.value = '';
}

// Escape key closes any open modal
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (document.getElementById('import-modal').classList.contains('show')) { closeImportModal(); return; }
  if (document.getElementById('export-modal').classList.contains('show')) { closeModal(); }
});

async function loadPlaylist(mode) {
  const pathEl = document.getElementById('inp-playlist-path');
  const path = pathEl.value.trim();
  const resultEl = document.getElementById('import-result');

  let r, d;
  if (window._pendingPlaylistData) {
    // File was read directly in browser via file input
    resultEl.innerHTML = '<span style="color:var(--muted)">Importing…</span>';
    r = await fetch('/api/load-playlist-data', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({data: window._pendingPlaylistData})
    });
    window._pendingPlaylistData = null;
    pathEl.placeholder = 'Click 📂 to browse for playlist.json';
  } else if (path) {
    // Manual path typed in
    resultEl.innerHTML = '<span style="color:var(--muted)">Loading…</span>';
    r = await fetch('/api/load-playlist', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({path})
    });
  } else {
    alert('Select a playlist.json file first');
    return;
  }
  d = await r.json();

  if (!d.ok) {
    resultEl.innerHTML = `<span style="color:var(--red)">✗ ${esc(d.error)}</span>`;
    return;
  }

  if (mode === 'replace') {
    selected = [];
    autoSelected = [];
  }

  const autoSet = new Set(d.auto_matched || []);
  const existing = new Set([...selected, ...autoSelected]);
  let addedManual = 0, addedAuto = 0;
  for (const id of d.matched) {
    if (!existing.has(id)) {
      existing.add(id);
      if (autoSet.has(id)) {
        autoSelected.push(id);
        addedAuto++;
      } else {
        selected.push(id);
        addedManual++;
      }
    }
  }
  const added = addedManual + addedAuto;

  refreshAllCards();
  updateSelectionUI();

  const notFoundHtml = d.not_found.length
    ? `<div style="color:var(--red);margin-top:6px;">✗ Not found (${d.not_found.length}): ${d.not_found.slice(0,5).map(esc).join(', ')}${d.not_found.length > 5 ? '…' : ''}</div>`
    : '';

  resultEl.innerHTML = `
    <div style="color:var(--green)">✓ Imported ${added} albums (${d.matched.length}/${d.total} matched)</div>
    <div style="color:var(--muted);font-size:10px;margin-top:2px;">${addedManual} manual · ${addedAuto} auto</div>
    <div style="color:var(--muted);font-size:10px;margin-top:4px;">Saved: ${esc(d.created)}  |  Server: ${esc(d.server)}</div>
    ${notFoundHtml}
    <div style="margin-top:10px;">
      <button onclick="closeImportModal()" style="width:100%;">Done</button>
    </div>`;
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print(f"\n🎵 Jellyfin Album Picker running at http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
