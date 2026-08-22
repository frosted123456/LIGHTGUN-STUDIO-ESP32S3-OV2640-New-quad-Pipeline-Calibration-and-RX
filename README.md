# OV2640 Lightgun Overlay for OpenFIRE

An IR lightgun built from a **Freenove ESP32-S3-WROOM CAM** and four IR LEDs,
running the ESP32-S3 port of
[OpenFIRE](https://github.com/alessandro-satanassi/OpenFIRE-Firmware-ESP32) with
its PAJ7025 sensor replaced by an OV2640 camera, on-chip blob detection, and a
calibrated aiming pipeline.

You get a desktop app that walks you through tuning the camera, calibrating,
fine-tuning to your iron sights, and measuring the result.


Please test. You get extremely accurate aim with snappy aim. No springy effect when fine tunning is properly performed. Give me your feedback! 
---

## What you need

**Hardware**

| Part | Specification |
|---|---|
| Board | Freenove ESP32-S3-WROOM CAM, **N8R8** — 8 MB flash, 8 MB **octal** PSRAM |
| Camera | OV2640 module **without the IR-cut filter** — sold as "NV" / "night vision"  **75 mm** FPC ribbon between camera and board |
| Lens filter | IR long-pass over the lens — **800 nm recommended (non tested)**, 700 nm works |
| IR LEDs | 4 × **850 nm**, one per screen corner, **5 W class recommended** |
| Trigger | A button wired per OpenFIRE's pinout |
| Cable | One USB-C cable to the board's **native USB** socket (see below) |

**Why those wavelengths.** 850 nm LEDs with an 800 nm long-pass filter is a
matched pair: the LEDs pass, room light and screen glow do not. Do not
substitute 940 nm LEDs — silicon sensitivity falls off steeply past 850 nm, so
you lose most of the signal you are filtering for. A 700 nm filter works but
lets more of the visible red end through, which raises the blob noise floor you
will be fighting in step 2.

**The camera must be the IR-cut-free version.** A normal OV2640 has a filter
glued over the sensor that blocks exactly the light you want. Some modules let
you scrape it off; buying the NV version is easier.

The LED rectangle does not have to match your screen — the calibration measures
whatever shape it is. It does need to be rigid. Tape sags as the resistors warm
up, and the calibration screen will tell you if it moved while you were working.
Not tested or supported on diamond shape. 

**Software**

Fully tested using Visual Studio Code. Procedure, below not fully tested but should work.  

You need Python 3.9+, PlatformIO Core, `git`, and two Python packages. Run the
block for your system.

*Windows* — install [Python](https://www.python.org/downloads/) (tick "Add
python.exe to PATH") and [Git for Windows](https://git-scm.com/download/win),
then in a new terminal:

```
python -m pip install --upgrade platformio
python -m pip install pyserial numpy
```

*Linux (Debian/Ubuntu)*:

```
sudo apt install python3 python3-pip python3-tk git
python3 -m pip install --upgrade platformio pyserial numpy
```

*macOS* — install the [python.org](https://www.python.org/downloads/) build
rather than the system Python, which has no working Tkinter:

```
python3 -m pip install --upgrade platformio pyserial numpy
```

Then confirm all four in one go. Every line must print, with no traceback:

```
git --version
python -m platformio --version
python -c "import serial, numpy, tkinter; print('python deps ok')"
```

If `platformio` is not found afterwards, its scripts folder is not on your PATH;
`python -m platformio` works regardless and every command below can be written
that way.

Also download the [OpenFIRE desktop app](https://github.com/TeamOpenFIRE/OpenFIRE-App).
It is only used to set buttons and pins in step 1 — it is not needed to build. Add it in the root of the project. 

---

## Build and flash

> **Do step 1 first.** This folder does not contain OpenFIRE, so a fresh
> checkout has nothing to compile until the patcher has run. Building first
> fails with `UnknownBoard: Unknown board ID ...` or a wall of missing headers,
> neither of which points at the real cause. The build now stops with a plain
> message instead, but the fix is the same: run step 1.

**1. Get OpenFIRE and apply the overlay — one command.**

```
python tools/patch_openfire.py --fetch
```

That clones the firmware into `OpenFIRE-Firmware-ESP32/` next to this project,
checks out the exact commit the overlay is built against, and patches it.

**About that pinned commit.** The overlay replaces a block of OpenFIRE's own
aiming code, so the patch is tied to one upstream revision:
**`f8f9bf265c48` (2026-07-17)**, recorded with file fingerprints in
`patches/upstream.json`.

Upstream `main` has since moved on — several commits rework the camera layer for
multi-camera support and move the very code this overlay replaces, so **the patch
will refuse on current `main`**. That is not a silent failure: it names the line
it could not place and **writes nothing at all**, leaving your checkout exactly
as it was. `--fetch` avoids the problem by pinning.

If you already have a checkout:

```
git -C OpenFIRE-Firmware-ESP32 checkout f8f9bf265c48
python tools/patch_openfire.py OpenFIRE-Firmware-ESP32
```

The tree should end up as:

```
<this folder>/
    platformio.ini
    lib/  tools/  patches/  boards/
    OpenFIRE-Firmware-ESP32/
        lightgun/src/
        shared_boards/
        shared_lib/
```

The patcher keeps `.orig` copies of both files it edits, and is safe to re-run.

Check it landed before starting a build:

```
python tools/check_setup.py
```

It prints `setup OK`, or names exactly what is still missing. The build runs the
same check itself and refuses rather than failing later in a confusing place.

**2. Plug into the right USB socket.**

The board has two USB-C sockets. Use the one wired straight to the ESP32-S3's
own USB peripheral — **not** the USB-to-UART bridge. Everything after the flash
depends on this: the gun's HID mouse and the `~` serial channel that all the
tools talk to both live on the native port. Flashing over the UART port will
appear to work and then no tool will find the gun.

How to tell them apart without reading the silkscreen: the UART port enumerates
as a serial device the moment you plug it in, with the board held in reset or
not. The native port only enumerates once firmware is running, and after this
firmware is flashed it also shows up as a mouse.

Find the port:

```
python tools/list_ports.py
```

**3. Build and upload.**

```
pio run -e combined_s3_freenove -t upload --upload-port COM8
```

Replace `COM8` with what `list_ports.py` reported — on Linux
`/dev/ttyACM0`, on macOS `/dev/cu.usbmodem*`. Dropping `--upload-port` lets
PlatformIO guess, which picks the wrong socket on a two-port board often enough
to be worth not doing.

If the upload cannot start, hold **BOOT**, tap **RESET**, release **BOOT**, and
run it again. On Linux, `Permission denied` on the port means you are not in the
`dialout` group: `sudo usermod -aG dialout $USER`, then log out and back in.

**4. Confirm it booted.** Open the serial monitor (`pio device monitor`). The
banner names the build, `SHIP` or `DIAG`. If you see OpenFIRE start with no
`LIGHTGUN` line, the overlay is not in the build.

---

## Set it up

```
python tools/gun_studio.py
```

Five steps, in order. Order matters: aim accuracy is limited by camera noise, so
tuning before calibrating is not optional — the app blocks step 3 if the noise
floor is too high.

**1 — Buttons & pins.** Opens the OpenFIRE desktop app, which needs the serial
port to itself. Set your trigger and pins there, then just close it — Studio
waits for it to exit, takes the port back and reconnects on its own. `Reconnect`
in the header is there if you ever need to force it.

**2 — Camera tuning.** Exposure, gain and threshold. Aim for a blob noise floor
**under 0.30 px**; 0.60 is the limit. `Auto` sweeps for you.

**3 — Aim calibration.** Five dots at each of two or three distances. Aim, pull
the trigger four times per dot. Stepping back between rounds is **required** —
at one distance the boresight and the screen mapping cannot be separated, and
the fit will refuse. It ends by sending the calibration to the gun and reading
it back to confirm.

**4 — Fine tune.** Lines the cursor up with your **iron sights**, which is not
where the camera points. Shoot the ring, nudge with the four arrow buttons if
needed, step back, repeat. Two positions are needed because a sight offset can be
angular (grows with distance) or parallax (constant), and the wrong correction is
worse than none. `LEAD ±` trades latency for overshoot — raise it while the cursor
trails you, stop as soon as it overshoots when you reverse direction. You will see a big difference here

**5 — Verify.** Shoots a nine-point grid and reports the error of the pipeline
alone against the error after the OS. If the two agree, any remaining error is
the calibration's, not your driver's.

**F9** freezes the cursor while Studio is open, so the gun stops fighting you
for the mouse. It is released automatically for steps 3 to 5 and restored when
you quit. It is never saved — a power cycle always gives you the cursor back.

---

## Troubleshooting

**Nothing on the serial port / the tools cannot find the gun**

```
python tools/list_ports.py
python tools/aim_probe.py --port COM5
```

`aim_probe` reports what the gun answers and what it does not. Use one cable, on
the board's **native USB** port. Do not run the OpenFIRE app at the same time —
it takes the port for itself.

**The reticle wanders, or the LEDs are hard to lock onto**

You are probably too close. When an LED is partly off the sensor its reported
centre is pulled inward, so the shape distorts and lock becomes unstable. The
calibration screen shows **TOO CLOSE — STEP BACK** when it happens. A quad span
around 65 px is comfortable.

**Blob noise floor will not come down**

Check the IR-pass filter is fitted and no sunlight or incandescent lamp is in
frame. Then re-run step 2's `Auto`. Bright LEDs with a short exposure beat dim
LEDs with a long one.

**Calibration refused**

- *"all shots were taken at effectively one distance"* — you did not step back far
  enough. Roughly 50 % further is enough.
- *"the LEDs are running off the edge of the camera"* — step back.
- *"fitted LED rectangle is implausible"* — the resolver locked onto something
  other than your four LEDs. Raise the threshold in step 2 and retry.

**`UnknownBoard: Unknown board ID 'ESP32-S3-WROOM-1-DevKitC-1-N8R2'`**

An older copy of this project looked for the board definition inside the
OpenFIRE checkout, so it could not be found before that checkout existed. The
definition now ships in `boards/` and resolves on its own. If you still see
this, your `platformio.ini` line should read `boards_dir = boards`.

**The build stops with `SETUP INCOMPLETE`**

That is the pre-build guard, and the box names which of the three states you are
in: OpenFIRE not downloaded, downloaded but not patched, or a partly-populated
checkout. Each one is fixed by running the command it prints. `python
tools/check_setup.py` runs the same check without starting a build.

**`undefined reference to 'cam_patch_chunk_cb'` at link time**

Your copy of `lib/esp32-camera-ov2640/` is missing its headers. PlatformIO only
links a private library that something includes, and `esp_camera.h` lives in
that library's `driver/include/` — with the headers gone the whole library is
silently left out of the build, and the first sign of it is this link error.
The folder should hold **17 files**, including `driver/include/`,
`driver/private_include/`, `sensors/private_include/` and `target/`. `python
tools/check_setup.py` reports this before a build starts.

**The patcher says "CANNOT PATCH"**

Your OpenFIRE is not the pinned commit. Either use `--fetch` into a fresh
folder, or `git checkout f8f9bf265c48` in the copy you have. Rebasing the
overlay onto a newer `main` is a real (if not huge) piece of work: upstream is
building its own camera abstraction, so a future version may offer a cleaner
seam than patching.

**Calibration is not saved**

The results screen says either `INSTALLED ON THE GUN` or `NOT INSTALLED` with a
reason. It reads the value back from the gun before claiming success. If it
could not send, the line it prints can be pasted into a serial monitor by hand —
note the leading `~`, which is what makes the gun claim the line instead of
passing it to OpenFIRE:

```
~aimcal=0.512161,0.535141,0.401183,1.241383,12.156,9.125
```

**Aim was right, then hours later it is off by about a centimetre**

Almost certainly mechanical, not software — the aim path was measured over two
simulated hours of a motionless gun and drifts under 0.1 screen px, and the
32-bit microsecond clock wrap at ~72 minutes moves the cursor by 0.0000 px.
Both are asserted by `hostcheck/long_run_drift_test.cpp`.

What does move is the hardware, and the boresight is an *angular* reference, so
the master gain (~26 screen px per camera px) multiplies any mechanical shift:

| the camera shifts in its mount by | cursor moves |
|---|---|
| 0.25 sensor px (0.08 deg) | 3.6 px |
| 1 sensor px (0.31 deg) | 14 px |
| 2 sensor px (0.62 deg) | 28 px |

A third of a degree is a centimetre on screen. A camera that can settle in a
taped or printed mount as it warms will do this. The LED rig moving matters far
less — a whole-rig 2 mm shift is 11 px, one LED slipping 5 mm is 14 px — because
the calibration measures the rectangle rather than assuming it.

Fix the camera mount rigidly before chasing this in software. Re-running step 4
takes about a minute and corrects it either way.

**Aim drifts as you move closer or further away**

The fine-tune split needs two positions far enough apart. Redo step 4 and step
well back for the second one.

**The gun stops moving the cursor**

Studio froze it and did not get to restore it. Reopen Studio and press **F9**,
or unplug and replug — the freeze is never stored.

---

## Verifying a build

```
bash hostcheck/check.sh
```

Compiles every build combination, runs the geometry, protocol and NVS tests,
and renders each GUI screen headlessly. Needs `g++`, and `Xvfb` for the GUI
checks (they are skipped with a notice if it is missing). Every line should end
in `OK`.

---

## Useful serial commands

All are prefixed `~` on the native USB port.

| Command | Effect |
|---|---|
| `~ping` | Is the overlay alive, and is a calibration loaded |
| `~aimcal?` | Print the active calibration |
| `~aimcal=...` | Install and save a calibration |
| `~aimhid=0` / `=1` | Freeze / release the cursor (never saved) |
| `~cam?` | Camera settings, including `lead` |
| `~cam=thr:60,aec:40` | Tune the camera live |
| `~cam=lead:10` | Latency lead in ms, 0–30 |
| `~camsave` | Persist camera settings and lead |

See `NOTICE.md` for third-party code and licences.
