# 🎵 Jellyfin Album Picker

A local web app for browsing your Jellyfin music library, handpicking albums, and exporting each one as a single merged WAV file — numbered 00–99, sorted oldest to newest by release year.

---

## Requirements

- **Python 3.10+** with `python3-venv`
- **ffmpeg** (must be on your `$PATH`)
- A running **Jellyfin** server (10.8+ recommended)

---

## Setup

```bash
# 1. Create a virtual environment
python3 -m venv venv

# 2. Activate it
source venv/bin/activate          # Linux/macOS
# venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the app
python app.py

# Optional: run on a different port
python app.py 8080
```

Open **http://localhost:5000** in your browser.

To run it again later:
```bash
source venv/bin/activate
python app.py
```

### Optional: run.sh (Linux/macOS)

A `run.sh` script is included for convenience. It sets up the virtual environment on first run, then starts the app and opens your browser automatically each time.

```bash
chmod +x run.sh   # once, to make it executable
./run.sh
```

You can also double-click it in a file manager if it's configured to run shell scripts.

---

## Connecting to Jellyfin

Enter your server URL (e.g. `http://192.168.1.100:8096`) and either:

- **Username + Password** — recommended, uses Jellyfin's standard auth
- **API Key** — generate one in Jellyfin Dashboard → API Keys, paste it in the API Key field and leave username/password blank

> **Note:** Your Jellyfin user needs **"Allow media downloading"** enabled (Dashboard → Users → your user → Media Playback). Without this the download endpoint returns 403.

---

## Browsing & Selecting

### Grid view
Your full album library loads as a card grid with cover art, artist, year, track count, duration, and play count. Click any album to select it — it gets a number badge showing its export position (0-based, matching the output filename).

- **Yellow badge / border** = manually picked
- **Teal badge / border** = auto-filled

### Genre view
Click **♬ Genre** in the toolbar to switch to a genre-grouped layout. Genres are sorted by total play count (your most-listened genres first). Use **⊟ Collapse all** to scan genre names quickly, then expand the one you want. Clicking a genre header collapses/expands it. Search also filters by genre name in this view.

### Sorting
Sort by: Name, Artist, Year, Track count, Play count, Last played. Play count and Last played default to descending (most listened first).

---

## Selecting Albums

**Manual picks (yellow):** Click any album. Click again to deselect. There is no hard cap — the counter goes red above 100, but you can still export however many you want.

**Auto-fill (teal):** Click **✦ Auto-fill** to automatically fill remaining slots up to 100. The algorithm:
- ~80% weighted by play count (albums you listen to more are more likely, but not guaranteed)
- ~20% reserved for unplayed albums so you always discover something

Remove auto-filled albums you don't want with ✕, then hit **✦ Auto-fill** again to top back up. Use **↺ Redo** to replace the entire auto selection with a fresh random batch.

**Sidebar sections:**
- **MY PICKS** (yellow): your manual choices — drag to reorder, individual ✕ remove. "✕ Clear" clears only manual picks.
- **AUTO** (teal): auto-filled albums, showing ▶ or ◌ play indicator. "✕ Clear" clears only auto picks.

---

## Exporting

1. Optionally enter an **output directory** — leave blank to default to `~/Downloads/jellyfin-export`
2. Click **Export →**
3. Watch the live progress modal

### What the export does, in order

1. **Sorts** your selection oldest → newest by release year (sets the 00–99 numbering)
2. **Downloads** each album's tracks in parallel (8 simultaneous connections per album)
3. **Converts** each track to PCM WAV using all CPU cores in parallel — this ensures mixed-format albums (FLAC + MP3 + AAC) always produce a complete, correct output file
4. **Concatenates** all tracks into one WAV per album
5. **Saves** a `playlist.json` to the output folder

### Output filenames
```
00-Artist-(Year)-AlbumName.wav
01-Artist-(Year)-AlbumName.wav
...
99-Artist-(Year)-AlbumName.wav
```

If an album has no release year it sorts to the end and shows `(unknown)`.

### Export modal

- **Progress bar** — album-level progress (N of total)
- **Elapsed / ETA** — smoothed time estimate using a rolling average; stabilises after the first album
- **Live ticker** — current file downloading, bytes so far, speed
- **Log** — colour-coded full history, stays open after export finishes

Closing the modal while an export is running will ask for confirmation. The export always continues in the background.

Re-running an export skips any WAV files that already exist — safe to resume after interruption.

---

## Saving & Importing Playlists

After every export a `playlist.json` is automatically saved alongside the WAV files. It records the server URL, date, and each album's ID, name, artist, year, and play count.

To restore a previous selection:

1. Click **⬆ Import** next to the Export button
2. Enter the path to `playlist.json`
3. Choose:
   - **↺ Replace selection** — clears everything and loads the saved list as manual picks
   - **⊕ Merge with current** — adds saved albums to your current selection, skipping duplicates

Matching tries Jellyfin item ID first, then falls back to name + artist — so it works even after a server migration.

---

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Escape` | Close any open modal |

---

## Tips

- The play count on each card is the **sum of all track-level plays** — more accurate than Jellyfin's album-level count which is often 0.
- Albums with no release year sort to the end of the export and show `(unknown)` in the filename.
- The ETA says "calculating…" for the first few percent, then settles into a stable estimate.
- If a download stalls, the live ticker will freeze — a reliable sign something is stuck rather than just slow.
