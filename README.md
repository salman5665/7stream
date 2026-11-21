# 7Stream - Android Video Streaming Server

<p align="center">
  <img src="assets/logo.png" alt="7Stream Logo" width="160" style="border-radius: 28px;" />
</p>

**7Stream** is a high-performance, standalone Android video-streaming server and mobile GUI application written in Python using **Flet** and **aiohttp**.

It turns an Android smartphone or tablet into a portable, battery-efficient local media streaming server. 7Stream lets laptops, Smart TVs, tablets, and media players (such as VLC, MX Player, and Kodi) stream movies and videos directly from the phone's storage over **direct Mobile Hotspot (completely offline without internet or router)** or over a local **Wi-Fi Router network**.

---

## Key Features

- **Dual Connection Modes:**
  - **Mobile Hotspot Mode (100% Offline):** Stream directly from your phone to a laptop, TV, or tablet anywhere—on a flight, camping, road trip, or during power/internet outages. Does not require mobile data or an external Wi-Fi router.
  - **Wi-Fi Router Mode:** Connect to an existing home or office Wi-Fi network and stream across the local LAN.
- **Offline Hotspot IP Detection:**
  - Direct local network interface inspection using Linux/Android `SIOCGIFADDR` ioctl (`ap0`, `swlan0`, `wlan1`, `wlan0`, `rndis0`), `ip addr` subprocess parsing, and Android's default `192.168.43.1` fallback. Never fails even when mobile data is turned off.
- **High-Performance Async HTTP Engine (`aiohttp`):**
  - Runs in a background daemon thread keeping the Flet mobile UI fluid and responsive.
  - Natively supports **HTTP Byte-Range Requests (`206 Partial Content`)** via `web.FileResponse`—enabling instant, freeze-free video seeking, skipping, and fast-forwarding in VLC and web browsers.
- **Pure-Python QR Code Engine:**
  - Generates RFC 2083-compliant PNG QR codes in memory using standard library `zlib` and `struct`.
  - Avoids heavy native C-extension dependencies like Pillow (`PIL`), preventing compilation failures during `flet build apk`.
- **Automatic VLC Playlist (`/playlist.m3u`):**
  - Recursively scans the selected media directory and produces a ready-to-stream `#EXTM3U` playlist with full video titles and direct streaming URLs.
- **Dark-Mode Responsive Web Interface:**
  - Served directly from the phone. Features folder navigation, breadcrumbs, live search filtering, file size badges, one-tap stream link copying, and an inline HTML5 video player modal.
- **Strict Path-Traversal Security:**
  - Protects phone filesystem using `Path.resolve().relative_to(...)` boundary checks.

---

## Project Structure

```
7stream/
├── assets/
│   ├── icon.png         # App launcher icon (512x512)
│   ├── logo.png         # Brand logo (square PNG)
│   └── logo.svg         # High-resolution vector logo
├── main.py              # Complete application (Server, UI, QR Generator, Network Engine)
├── requirements.txt     # Pure-Python Android-compatible dependencies
├── .gitignore           # Git ignore configuration
└── README.md            # Build instructions, permissions, and usage guide
```

---

## Dependencies (`requirements.txt`)

7Stream uses only pure-Python wheels compatible with Android packaging tools:

```txt
flet
aiohttp
qrcode
```

Install locally:
```bash
pip install -r requirements.txt
```

---

## How to Install on Your Android Phone

There are three ways to run and install 7Stream on your Android device:

### Option 1: Build the Standalone APK (Recommended for Everyday Use)

Build an APK that installs directly onto your phone like any standard Android app.

#### 1. Prerequisites on your PC
- **Python 3.10+**
- **Flutter SDK** (managed automatically by Flet CLI)
- **Java Development Kit (JDK 17+)**
- **Android SDK** (Android Studio Command Line Tools)

#### 2. Build the APK Command
Run the following command inside this repository:

```bash
flet build apk --project "7stream" --org "com.sevenstream.app" --product "7Stream" --permissions INTERNET,ACCESS_WIFI_STATE,CHANGE_WIFI_STATE,ACCESS_NETWORK_STATE,READ_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,WAKE_LOCK
```

#### 3. Install on Phone
Once the build completes, the APK is located at:
```
build/apk/app-release.apk
```

Transfer it to your phone using any of these methods:
- **Direct USB Cable / ADB:**
  ```bash
  adb install -r build/apk/app-release.apk
  ```
- **File Transfer:** Copy `app-release.apk` to phone storage via USB cable, Google Drive, Telegram, or WhatsApp, tap the file on your phone, and tap **Install**.

---

### Option 2: Run via Termux (Instant No-Build Method on Phone)

If you don't have Android SDK/Flutter installed on your PC, you can run the server directly on your phone using **Termux**:

1. Install [Termux from F-Droid](https://f-droid.org/en/packages/com.termux/).
2. Open Termux and install Python and Git:
   ```bash
   pkg update && pkg install python git -y
   ```
3. Clone or copy your repository onto the phone:
   ```bash
   git clone https://github.com/<your-username>/7stream.git
   cd 7stream
   pip install aiohttp qrcode
   ```
4. Grant Termux storage access:
   ```bash
   termux-setup-storage
   ```
5. Run the server:
   ```bash
   python main.py
   ```

---

### Option 3: Flet App Viewer (Rapid Testing)

1. Install **Flet** from Google Play Store on your phone.
2. Run your app on your PC with the `--ip` flag on your local Wi-Fi:
   ```bash
   flet run --ip
   ```
3. Scan the terminal QR code using the Flet app on your phone to run the app immediately.

---

## Android 11+ "All Files Access" Setup

Android 11 (API 30+) enforces Scoped Storage. To allow 7Stream to stream movies stored in `/storage/emulated/0/Movies`, `/sdcard/Download`, or on an SD card:

1. Install and open the **7Stream** app on your phone.
2. Open Android **Settings** → **Apps** → **Special app access** (or search for *"All files access"* in Settings).
3. Select **All files access** (or **Manage all files**).
4. Locate **7Stream** in the list and toggle **Allow access to manage all files** to **ON**.
5. Return to 7Stream. The app can now stream videos from any folder on the device.

---

## How to Connect & Stream

### Mode 1: Direct Mobile Hotspot Mode (100% Offline)
Use this mode when traveling, camping, in a hotel without Wi-Fi, or during internet/router outages:

1. On your phone, turn **ON** your **Personal Hotspot** (Mobile Data / Cellular can remain **OFF**).
2. Connect your laptop, Smart TV, tablet, or another phone to your phone's Wi-Fi hotspot.
3. Open 7Stream, select **Mobile Hotspot Mode**, verify or select your video folder, and tap **Start Server**.
4. On your laptop or TV:
   - **Scan the QR Code** displayed on the phone screen with a camera, or
   - Open a browser and navigate to `http://192.168.43.1:8080`.

### Mode 2: Wi-Fi Router Mode
Use this mode when at home or in an office with an existing Wi-Fi router:

1. Ensure your phone and your client devices (laptop, Smart TV, PC) are connected to the **same Wi-Fi router**.
2. Open 7Stream, select **Wi-Fi Router Mode**, and tap **Start Server**.
3. Point any client browser to `http://<detected-ip>:8080` or scan the live QR code.

---

## Playing in VLC Media Player

VLC Media Player supports native HTTP byte-range seeking and automatic playlist streaming:

### Method A: Stream the Entire Video Library via Playlist (.m3u)
1. In 7Stream, tap **Copy VLC Playlist Link (.m3u)** (or use `http://<phone-ip>:8080/playlist.m3u`).
2. Open **VLC Media Player** on your PC, Mac, or Smart TV.
3. Go to **Media** → **Open Network Stream...** (shortcut `Ctrl + N` on Windows/Linux, `Cmd + N` on macOS).
4. Paste the URL:
   ```
   http://192.168.43.1:8080/playlist.m3u
   ```
5. Click **Play**. VLC will immediately populate its playlist with all video files from your phone's folder with titles. You can skip between videos and seek freely.

### Method B: Stream Individual Videos
- In the 7Stream web interface, click **🔗 Copy** next to any video, then paste the direct stream link into VLC's network stream dialog.
- Alternatively, click **▶ Play** to watch directly inside the browser using the responsive HTML5 video player.

---

## Supported Video Formats

7Stream streams any video container natively supported by your client player or browser:
- Containers: `.mp4`, `.mkv`, `.avi`, `.mov`, `.webm`, `.ts`, `.flv`, `.m4v`, `.3gp`, `.wmv`, `.m2ts`, `.vob`, `.ogv`.
- Hardware seeking: Fully functional across all formats via HTTP 206 Partial Content byte ranges.

---

## Pushing to GitHub

To push this repository to your personal GitHub account:

```bash
# 1. Initialize git
git init

# 2. Add all files
git add .

# 3. Create initial commit
git commit -m "Initial commit: 7Stream standalone Android video streaming server"

# 4. Set default branch to main
git branch -M main

# 5. Add your GitHub remote repository (replace with your repo URL)
git remote add origin https://github.com/<your-username>/7stream.git

# 6. Push to GitHub
git push -u origin main
```
