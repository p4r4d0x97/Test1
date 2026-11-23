# Harness Camera Station

When the operator scans a barcode, the app takes one photo from each camera (1–3 or more), puts them side by side in a fixed left-to-right order, and saves the result as `<barcode>.jpg`.

## Files
```
HarnessCam\
  harness_cam.py         the app
  start.bat              starts the app
  install_packages.bat   installs the Python packages from the wheels folder (offline)
  requirements.txt       package list (used by install_packages.bat)
  README.md              this guide
  wheels\                Python packages for Windows 64-bit, Python 3.11 / 3.12 / 3.13
```
`config.json` is created next to `harness_cam.py` when you save settings.

## Install on an offline PC
1. Install **Python 64-bit** (3.11, 3.12 or 3.13) with the "Windows installer (64-bit)" from python.org, which also works offline. Tick **"Add Python to PATH"**.
2. Copy the whole `HarnessCam` folder, including `wheels`, to the PC, for example `C:\HarnessCam`.
3. Double-click **`install_packages.bat`**. It needs no internet.
4. Start the app with **`start.bat`**. To start it with Windows, press Win+R, type `shell:startup`, and put a shortcut to `start.bat` in that folder.

Getting newer wheels later (on a PC with internet):
`pip download -d wheels --only-binary=:all: --platform win_amd64 --python-version 3.12 -r requirements.txt`

## First setup
1. Plug in all the cameras and open **⚙ Settings → Cameras**.
2. Click **Detect USB cameras**, then **Identify**. You get a snapshot from each camera, so you can tell which physical camera is which.
3. Use **+ Add slot** to add one slot per camera, and pick a camera for each: slot 1 = Left, 2 = Middle, 3 = Right. Use **▲ ▼** to swap positions.
4. Set **Rotate** if needed. Use **Crop L/R px** to cut off the area where two cameras overlap.
5. On the **General** tab, set the **Photo folder** and the **Log folder** (they can be different), then click **Save & apply**.

## How the camera order stays fixed
Each slot is tied to the camera's hardware ID (serial number, or the USB port if the camera has no serial), not to the camera number Windows hands out. Cameras without a serial number must always stay in the same USB port, so label the ports and cables.
If a camera is missing, its badge turns red and **scanning is blocked**, so a photo is never saved with cameras missing or out of order.

LAN cameras: set the slot type to `ip` and enter the stream URL (for example `rtsp://admin:pass@192.168.1.50:554/stream1`). Give the camera a fixed IP address.

## Scanning
- The scanner must act as a keyboard and send Enter after the code, which is the default for most scanners.
- After a successful scan, a green **SCAN OK** popup shows for 1.5 s and closes by itself. The next scan works straight away.
- If the barcode already exists, the app saves `BARCODE_2.jpg`. You can change this to overwrite or block in Settings.
- The station ID is the computer name (hostname).

## Log file
`scan_log.txt` is saved in the **Log folder** (Settings → General), one line per event:
```
2026-11-24 08:15:02 | TABLE-PC-01 | HX-12345 | OK | C:\HarnessPhotos\HX-12345.jpg
2026-11-24 08:16:40 | TABLE-PC-01 | HX-12346 | ERROR: Middle camera not ready ...
2026-11-24 08:17:05 | TABLE-PC-01 | CAMERA RESET | Middle: reconnected
```
It keeps the newest 1000 lines and deletes the oldest automatically.

## Camera reset (no PC reboot)
Click a **red camera badge** to reset that camera, or **↻ Restart cameras** to reset all of them.
1. **Reconnect**: the app closes and reopens the camera. No admin needed, and it works for every user.
2. **USB restart**: if the camera still doesn't work, Windows restarts that USB device. This is the same as unplugging and replugging it, and the scanner and the other cameras are not affected.
   Windows shows the **admin login prompt (UAC)**. An admin types their username and password. If nobody logs in, only step 1 is done.
   The restart only touches devices that Windows lists as cameras.

**Auto-reconnect:** a camera that has been down for 30 s is reconnected automatically (step 1 only, so no login prompt appears by itself). You can change this in Settings.
If a reset doesn't help, the problem is physical: the cable, the USB port, or not enough USB power.

## Several USB cameras on one PC
Keep **Use MJPG** turned on. If a camera fails to open, plug the cameras into different USB controllers (front and back ports, or a PCIe USB card), or lower the resolution.
