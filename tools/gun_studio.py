#!/usr/bin/env python3
"""gun_studio.py -- the front door for setting up the lightgun.

    python tools/gun_studio.py

One window, five steps: buttons/pins, camera tuning, aim calibration, fine tune,
verify. Requires pyserial, numpy, tkinter.
"""
import os, sys, subprocess, threading, queue, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import aim_fit
from aim_calib import (parse_q, is_trigger, sigma_from_hold, find_gun,
                       SerialSource, FRAME_W, FRAME_H)

SIGMA_GOOD, SIGMA_OK = 0.30, 0.60      # px; gates step 3
APP_PORT_WAIT_S = 60.0                 # keep trying this long after their app exits
CAM_KEYS = ("thr", "aec", "agc", "boost")
CAM_RANGE = {"thr": (8, 200), "aec": (4, 400), "agc": (0, 30), "boost": (0, 1)}


def port_is_free(port):
    # Is the port openable right now. Never touches Link: the tick loop owns it.
    try:
        import serial
        s = serial.Serial(port, 115200, timeout=0.2)
        s.close()
        return True
    except Exception:
        return False


def take_port_back(proc, port, timeout_s=APP_PORT_WAIT_S, probe=port_is_free,
                   sleep=time.sleep, clock=time.monotonic):
    # Wait for their app to exit AND the port to open again; two separate waits
    # because a launcher can exit while the real app still holds the port.
    # Always returns, so the caller always gets to reconnect.
    try:
        if proc is not None:
            proc.wait()
    except Exception:
        pass
    if not port:
        return "no port"
    deadline = clock() + timeout_s
    while clock() < deadline:
        if probe(port):
            return "free"
        sleep(1.0)
    return "timeout"


# ---------------------------------------------------------------------------
# a serial link that owns the port and can be handed over to a child process
# ---------------------------------------------------------------------------
class Link:
    def __init__(self):
        self.src = None
        self.port = None
        self.frames = 0
        self.hist = []            # recent quads, for the live view and sigma
        self.last = {}
        self.replies = []
        self.hid_on = True        # the gun boots this way; we do not change it uninvited

    def connect(self, port=None):
        self.port = port or find_gun()
        if not self.port:
            return False
        self.src = SerialSource(self.port)
        self.src.start()
        return True

    def close(self):
        if self.src:
            try: self.src.close()
            except Exception: pass
            self.src = None

    def send(self, line):
        if not self.src: return
        # The '~' prefix is required: the gatekeeper on the shared serial only
        # claims lines starting with '~'; anything else goes to OpenFIRE.
        if not line.startswith("~"): line = "~" + line
        try: self.src.ser.write(("\n%s\n" % line).encode())
        except Exception: pass

    def pointer(self, on, remember=True):
        """Freeze or release the cursor; remember=False forces it on temporarily."""
        # Only possible while we own the serial port: force it on before handing over.
        if remember:
            self.hid_on = on
        self.send("~aimhid=%d" % (1 if on else 0))

    def pump(self):
        """drain the stream; keep the last ~2 s of quads"""
        if not self.src: return
        n = 0
        while n < 400:
            try: line = self.src.q.get_nowait()
            except queue.Empty: break
            n += 1
            if line.startswith("CAM:") or line.startswith("AIM:") or "CMD ok" in line:
                self.replies.append(line)
                # keep the label honest if the gun disagrees with us
                if "pointer FROZEN" in line: self.hid_on = False
                elif "pointer ON" in line:   self.hid_on = True
                if line.startswith("CAM: thr=") or "CMD ok" in line:
                    for tok in line.replace("|", " ").split():
                        if "=" in tok:
                            k, v = tok.split("=", 1)
                            if k in CAM_KEYS:
                                try: self.last[k] = int(v)
                                except ValueError: pass
                continue
            if is_trigger(line):
                self.last["trig"] = self.last.get("trig", 0) + 1
                continue
            pq = parse_q(line)
            if pq is None: continue
            q, gt = pq
            self.frames += 1
            self.hist.append((gt, q))
        cut = self.hist[-1][0] - 2.0 if self.hist else 0
        self.hist = [h for h in self.hist if h[0] >= cut][-400:]

    # ---- measurements ----------------------------------------------------
    def sigma(self):
        """blob noise with hand tremor removed; None until enough frames"""
        if len(self.hist) < 40: return None
        a = np.array([h[1] for h in self.hist[-120:]])
        # only use a stretch where the hand was reasonably still
        cen = a.mean(1)
        if max(np.ptp(cen[:, 0]), np.ptp(cen[:, 1])) > 12.0: return None
        return sigma_from_hold(a)

    def span(self):
        if not self.hist: return 0.0
        return aim_fit.quad_span(self.hist[-1][1])

    def fps(self):
        if len(self.hist) < 10: return 0.0
        dt = self.hist[-1][0] - self.hist[0][0]
        return (len(self.hist) - 1) / dt if dt > 0 else 0.0


# ---------------------------------------------------------------------------
# auto-tune: find an exposure/threshold that gives four stable blobs quietly
# ---------------------------------------------------------------------------
def auto_tune(link, log, stop):
    """Sweep aec x thr and apply the best: four-blob rate first, sigma breaks ties."""
    best = None
    aecs = [20, 30, 40, 60, 90]
    thrs = [40, 60, 80, 110, 150]
    total = len(aecs) * len(thrs)
    done = 0
    for aec in aecs:
        for thr in thrs:
            if stop.is_set(): log("auto-tune cancelled"); return None
            link.send("~cam=aec:%d,thr:%d" % (aec, thr))
            time.sleep(0.35)                       # let the sensor settle
            link.hist = []
            t0 = time.time()
            while time.time() - t0 < 0.9:
                link.pump(); time.sleep(0.02)
            done += 1
            n = len(link.hist)
            if n < 15:
                log("  aec=%-3d thr=%-3d  no frames" % (aec, thr)); continue
            sg = link.sigma()
            spans = [aim_fit.quad_span(h[1]) for h in link.hist]
            # every streamed frame already has 4 blobs, so fps IS the four-blob rate
            rate = n / 0.9
            score = (rate, -(sg if sg is not None else 9.9))
            log("  aec=%-3d thr=%-3d  %5.0f fps  span %5.1f  sigma %s"
                % (aec, thr, rate, np.mean(spans),
                   "%.3f" % sg if sg is not None else "  -  (hand moved)"))
            if best is None or score > best[0]:
                best = (score, aec, thr, sg, rate)
    if not best:
        log("auto-tune found NOTHING -- are all four LEDs in view?")
        return None
    _, aec, thr, sg, rate = best
    log("")
    log("best: aec=%d thr=%d  (%0.0f fps with four blobs, sigma %s)"
        % (aec, thr, rate, "%.3f" % sg if sg is not None else "not measured"))
    link.send("~cam=aec:%d,thr:%d" % (aec, thr))
    time.sleep(0.3)
    log("applied. Press 'Save to gun' to keep it across power cycles.")
    return (aec, thr)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def main():
    import tkinter as tk
    from tkinter import ttk, messagebox

    link = Link()
    root = tk.Tk()
    root.title("Lightgun Studio")
    root.configure(bg="#0d1117")
    root.geometry("1020x800")

    C_BG, C_FG, C_DIM, C_OK, C_WARN, C_BAD = "#0d1117", "#e6edf3", "#7d8590", "#39c26e", "#d8a13a", "#d24b4b"
    F = ("Segoe UI" if os.name == "nt" else "DejaVu Sans", 10)
    FB = (F[0], 11, "bold")
    FH = (F[0], 15, "bold")

    def lab(parent, text, font=F, fg=C_FG, **kw):
        return tk.Label(parent, text=text, font=font, fg=fg, bg=C_BG, **kw)

    # ---- header ---------------------------------------------------------
    head = tk.Frame(root, bg=C_BG); head.pack(fill="x", padx=16, pady=(14, 6))
    lab(head, "Lightgun Studio", FH).pack(side="left")
    st_conn = lab(head, "not connected", F, C_BAD); st_conn.pack(side="right")
    # the pointer toggle is a MODE, so the header always shows which one is active
    st_hid = lab(head, "", F, C_OK); st_hid.pack(side="right", padx=(0, 18))
    # How many distances to calibrate from. More than one is required: at a single
    # distance the boresight and the screen mapping are degenerate and the fit refuses.
    stance_n = tk.IntVar(value=3)

    body = tk.Frame(root, bg=C_BG); body.pack(fill="both", expand=True, padx=16, pady=8)

    # ---- left: the four steps -------------------------------------------
    left = tk.Frame(body, bg=C_BG); left.pack(side="left", fill="y", padx=(0, 18))
    step_rows = {}
    for n, (num, title, sub) in enumerate([
        (1, "Buttons & pins", "opens the OpenFIRE app"),
        (2, "Camera tuning",  "exposure, threshold, noise floor"),
        (3, "Aim calibration","five dots x three distances"),
        (4, "Fine tune",      "iron sights to cursor, and lead"),
        (5, "Verify",         "measures whose error it is")]):
        f = tk.Frame(left, bg=C_BG); f.pack(fill="x", pady=5)
        b = tk.Button(f, text="%d.  %s" % (num, title), font=FB, width=22, anchor="w",
                      bg="#161b22", fg=C_FG, activebackground="#21262d",
                      relief="flat", padx=10, pady=8)
        b.pack(fill="x")
        s = lab(f, "   " + sub, (F[0], 9), C_DIM, anchor="w"); s.pack(fill="x")
        step_rows[num] = (b, s)
        if num == 3:
            # distance count, next to the step it applies to
            rowd = tk.Frame(left, bg=C_BG); rowd.pack(fill="x", pady=(2, 0))
            lab(rowd, "   distances:", (F[0], 9), C_DIM).pack(side="left")
            for nval in (2, 3):
                tk.Radiobutton(rowd, text=str(nval), value=nval, variable=stance_n,
                               font=(F[0], 9), bg=C_BG, fg=C_FG, selectcolor="#161b22",
                               activebackground=C_BG, activeforeground=C_FG,
                               highlightthickness=0, bd=0,
                               command=lambda: step_rows[3][1].config(
                                   text="   five dots x %d distances" % stance_n.get())
                               ).pack(side="left")
            lab(rowd, "(2 is nearly as good and 20 fewer pulls)",
                (F[0], 8), C_DIM).pack(side="left", padx=(6, 0))

    # ---- right: the live panel ------------------------------------------
    right = tk.Frame(body, bg=C_BG); right.pack(side="left", fill="both", expand=True)
    toprow = tk.Frame(right, bg=C_BG); toprow.pack(fill="x")
    cv = tk.Canvas(toprow, width=380, height=280, bg="#010409", highlightthickness=1,
                   highlightbackground="#30363d")
    cv.pack(side="left", anchor="n")
    stats = tk.Frame(toprow, bg=C_BG); stats.pack(side="left", fill="both",
                                                  expand=True, padx=(14, 0))
    stat_vals = {}
    for k in ("frames", "rate", "quad span", "blob noise", "verdict"):
        r = tk.Frame(stats, bg=C_BG); r.pack(fill="x", pady=3)
        lab(r, k, F, C_DIM, width=11, anchor="w").pack(side="left")
        v = lab(r, "-", FB, C_FG, anchor="w", wraplength=230, justify="left")
        v.pack(side="left")
        stat_vals[k] = v

    logbox = tk.Text(right, height=8, bg="#010409", fg=C_DIM, font=("Consolas" if os.name=="nt" else "monospace", 9),
                     relief="flat", highlightthickness=1, highlightbackground="#30363d")
    logbox.pack(fill="both", expand=True, pady=(10, 0), side="bottom")
    def log(msg):
        logbox.insert("end", msg + "\n"); logbox.see("end")
    PY_LOG = log

    # ---- step actions ---------------------------------------------------
    def find_openfire_app():
        """Look where it actually tends to live before asking."""
        roots = [os.path.join(HERE, "..", ".."), os.path.expanduser("~")]
        for r in roots:
            for dirpath, dirnames, files in os.walk(os.path.abspath(r)):
                if dirpath.count(os.sep) - os.path.abspath(r).count(os.sep) > 4:
                    dirnames[:] = []
                    continue
                for f in files:
                    if f.lower() == "openfireapp.exe":
                        return os.path.join(dirpath, f)
        return None

    def step1():
        exe = find_openfire_app()
        if not exe:
            log("Could not find OpenFIREapp.exe automatically.")
            from tkinter import filedialog
            exe = filedialog.askopenfilename(title="Find OpenFIREapp.exe",
                                             filetypes=[("OpenFIRE app", "*.exe")])
            if not exe: return
        # Their app needs the port to itself, so hand it over and take it back.
        log("Releasing the port and starting the OpenFIRE app...")
        # release the pointer while we still own the port
        link.pointer(True, remember=False)
        link.close()
        st_conn.config(text="OpenFIRE app has the port -- close it to come back",
                       fg=C_WARN)
        try:
            proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
        except Exception as e:
            log("could not start it: %s" % e); reconnect(); return
        log("Set your pins and buttons there, then just CLOSE it.")

        # Come back on our own when their app exits; steps 3-5 already do this
        # because they call a tool we wrote.
        def take_the_port_back():
            why = take_port_back(proc, link.port or "")

            def done():
                if why == "timeout":
                    log("The port is still held after %ds -- something else has it."
                        % int(APP_PORT_WAIT_S))
                else:
                    log("OpenFIRE app closed; taking the port back...")
                reconnect()
            root.after(0, done)
        threading.Thread(target=take_the_port_back, daemon=True).start()
        log("Two settings that matter for us, in the profile:")
        log("  RunMode = Normal        (Average stacks extra smoothing on ours)")
        log("  serialARcorrection off  (it would re-correct an already-correct aim)")

    def step2():
        nb.select(tab_cam)

    def step3():
        sg = link.sigma()
        if sg is not None and sg > SIGMA_OK:
            from tkinter import messagebox
            if not messagebox.askyesno("Noise floor is high",
                    "Blob noise is %.2f px.\n\n"
                    "Aim error scales with this: 0.2 px gives about 16 px of error, "
                    "0.8 px gives about 46 px, and calibrating now bakes that in.\n\n"
                    "Tune the camera first?  (Yes = go to tuning, No = calibrate anyway)"
                    % sg):
                pass
            else:
                nb.select(tab_cam); return
        log("Handing the port to the calibration window...")
        link.pointer(True, remember=False)   # calibration reads the trigger as a click
        link.close()
        st_conn.config(text="calibrating...", fg=C_WARN)
        def run():
            try:
                subprocess.call([sys.executable, os.path.join(HERE, "aim_calib.py"),
                                 "--port", link.port or "",
                                 "--stances", str(stance_n.get())])
            except Exception as e:
                log("calibration failed to start: %s" % e)
            root.after(0, reconnect)
        threading.Thread(target=run, daemon=True).start()

    def _handoff(tool, label):
        """Force the pointer on for a child window, then restore the user's choice."""
        log("Handing the port to the %s window..." % label)
        link.pointer(True, remember=False)
        link.close()
        st_conn.config(text="%s..." % label, fg=C_WARN)
        def run():
            try:
                subprocess.call([sys.executable, os.path.join(HERE, tool),
                                 "--port", link.port or ""])
            except Exception as e:
                log("%s failed to start: %s" % (label, e))
            root.after(0, reconnect)
        threading.Thread(target=run, daemon=True).start()

    def step4(): _handoff("aim_finetune.py", "fine tune")
    def step5(): _handoff("aim_verify.py", "verify")

    step_rows[1][0].config(command=step1)
    step_rows[2][0].config(command=step2)
    step_rows[3][0].config(command=step3)
    step_rows[4][0].config(command=step4)
    step_rows[5][0].config(command=step5)

    # ---- tabs: camera tuning lives here ---------------------------------
    nb = ttk.Notebook(right)
    tab_cam = tk.Frame(nb, bg=C_BG)
    nb.add(tab_cam, text="  Camera  ")
    nb.pack(fill="x", pady=(12, 0))

    sliders = {}
    for k in CAM_KEYS:
        lo, hi = CAM_RANGE[k]
        r = tk.Frame(tab_cam, bg=C_BG); r.pack(fill="x", pady=1)
        lab(r, k, F, C_DIM, width=7, anchor="w").pack(side="left")
        v = tk.IntVar(value=lo)
        s = tk.Scale(r, from_=lo, to=hi, orient="horizontal", variable=v,
                     bg=C_BG, fg=C_FG, troughcolor="#161b22", highlightthickness=0,
                     length=260, showvalue=True)
        s.pack(side="left")
        sliders[k] = v
        def mk(kk, vv):
            def on(_=None): link.send("~cam=%s:%d" % (kk, vv.get()))
            return on
        s.config(command=mk(k, v))

    bar = tk.Frame(tab_cam, bg=C_BG); bar.pack(fill="x", pady=6)
    stop_flag = threading.Event()
    def do_auto():
        stop_flag.clear()
        log("auto-tune: sweeping exposure x threshold, about 30 s...")
        def run():
            r = auto_tune(link, lambda m: root.after(0, log, m), stop_flag)
            if r: root.after(0, lambda: (sliders["aec"].set(r[0]), sliders["thr"].set(r[1])))
        threading.Thread(target=run, daemon=True).start()
    tk.Button(bar, text="Auto-tune", command=do_auto, font=FB, bg="#1f6feb", fg="white",
              relief="flat", padx=14, pady=6).pack(side="left")
    tk.Button(bar, text="Save to gun", command=lambda: (link.send("~camsave"), log("~camsave sent")),
              font=FB, bg="#238636", fg="white", relief="flat", padx=14, pady=6).pack(side="left", padx=8)
    tk.Button(bar, text="Read from gun", command=lambda: link.send("~cam?"),
              font=F, bg="#161b22", fg=C_FG, relief="flat", padx=12, pady=6).pack(side="left")
    tk.Button(bar, text="Cancel", command=stop_flag.set,
              font=F, bg="#161b22", fg=C_FG, relief="flat", padx=12, pady=6).pack(side="right")

    # ---- pointer toggle ---------------------------------------------------
    def refresh_hid():
        if link.hid_on:
            st_hid.config(text="aim ON   (F9 to freeze)", fg=C_OK)
        else:
            st_hid.config(text="aim FROZEN   (F9 to release)", fg=C_WARN)

    def toggle_hid(_=None):
        if not link.src:
            log("Not connected -- cannot change the pointer.")
            return
        link.pointer(not link.hid_on)
        refresh_hid()
        log("Pointer %s." % ("released -- the gun drives the cursor again"
                             if link.hid_on else
                             "frozen -- the gun stops driving the cursor. "
                             "Steps 3 and 4 release it automatically."))

    # Window-scoped, not a global hook: focus the window and press F9.
    root.bind("<F9>", toggle_hid)
    root.bind("<KeyPress-F9>", toggle_hid)

    # ---- connection + live tick ------------------------------------------
    def reconnect():
        link.close()
        st_conn.config(text="looking for the gun...", fg=C_WARN)
        root.update_idletasks()
        if link.connect(link.port):
            st_conn.config(text="connected on %s" % link.port, fg=C_OK)
            log("connected on %s" % link.port)
            link.send("~cam?")
            link.send("~aimcal?")
            link.pointer(link.hid_on)      # the gun boots ON; we own this per session
            refresh_hid()
            refresh_hid()
        else:
            st_hid.config(text="", fg=C_DIM)
            st_conn.config(text="no gun found", fg=C_BAD)
            log("No gun found. Close the OpenFIRE app if it is open, then Reconnect.")

    tk.Button(head, text="Reconnect", command=reconnect, font=F, bg="#161b22",
              fg=C_FG, relief="flat", padx=10).pack(side="right", padx=10)

    def draw_view():
        cv.delete("all")
        W, H = 380, 280
        cv.create_rectangle(2, 2, W-2, H-2, outline="#30363d")
        if not link.hist:
            cv.create_text(W/2, H/2, text="no four-LED frames", fill=C_BAD, font=FB)
            return
        q = aim_fit.canon(link.hist[-1][1])
        for i, nm in enumerate(("TL", "TR", "BL", "BR")):
            x = 4 + (q[i][0]/FRAME_W)*(W-8)
            y = 4 + (q[i][1]/FRAME_H)*(H-8)
            cv.create_oval(x-4, y-4, x+4, y+4, fill="#ffd24a", outline="")
            cv.create_text(x+10, y-8, text=nm, fill=C_DIM, font=(F[0], 8), anchor="w")
        pts = [(4 + (q[i][0]/FRAME_W)*(W-8), 4 + (q[i][1]/FRAME_H)*(H-8)) for i in range(4)]
        for a, b in ((0,1),(1,3),(3,2),(2,0)):
            cv.create_line(*pts[a], *pts[b], fill="#3a7fbf")
        cv.create_line(W/2-6, H/2, W/2+6, H/2, fill="#e0803a")
        cv.create_line(W/2, H/2-6, W/2, H/2+6, fill="#e0803a")
        # a trail, so instability is visible rather than inferred
        for _, qq in link.hist[-60:]:
            c = qq.mean(0)
            x = 4 + (c[0]/FRAME_W)*(W-8); y = 4 + (c[1]/FRAME_H)*(H-8)
            cv.create_oval(x-1, y-1, x+1, y+1, outline="", fill="#1f6feb")

    def tick():
        link.pump()
        while link.replies:
            log(link.replies.pop(0))
        refresh_hid()      # the gun may have released itself via the escape hatch
        for k in CAM_KEYS:
            if k in link.last and sliders[k].get() != link.last[k]:
                sliders[k].set(link.last[k])
        draw_view()
        sg = link.sigma()
        stat_vals["frames"].config(text="%d" % link.frames)
        stat_vals["rate"].config(text="%.0f Hz" % link.fps())
        stat_vals["quad span"].config(text="%.1f px" % link.span())
        if sg is None:
            stat_vals["blob noise"].config(text="hold still to measure", fg=C_DIM)
            stat_vals["verdict"].config(text="-", fg=C_DIM)
        else:
            col = C_OK if sg <= SIGMA_GOOD else (C_WARN if sg <= SIGMA_OK else C_BAD)
            stat_vals["blob noise"].config(text="%.3f px" % sg, fg=col)
            # turn sigma into the number the user cares about
            err = 11.4 + (sg/0.05 - 1) * 1.2
            stat_vals["verdict"].config(
                text=("good -- ready to calibrate" if sg <= SIGMA_GOOD else
                      "usable, could be better" if sg <= SIGMA_OK else
                      "too noisy -- tune before calibrating"), fg=col)
        # step 3 is gated on step 2, and the label says why
        if sg is not None and sg > SIGMA_OK:
            step_rows[3][1].config(text="   blocked: blob noise %.2f px is too high" % sg, fg=C_BAD)
        else:
            step_rows[3][1].config(text="   five dots x %d distances" % stance_n.get(),
                                   fg=C_DIM)
        root.after(50, tick)

    log("Lightgun Studio. Step 1 sets up buttons; steps 2-4 are ours.")
    log("The gun keeps driving the cursor. Press F9 to freeze it while you work")
    log("in here; steps 3 and 4 release it on their own and put it back after.")
    log("Order matters: aim accuracy is limited by blob noise, so tune before you")
    log("calibrate. The app blocks step 3 if the noise floor is too high.")
    reconnect()
    root.after(50, tick)
    try:
        root.mainloop()
    finally:
        # unconditional: a traceback in the GUI must not leave the pointer frozen
        try: link.pointer(True, remember=False)
        except Exception: pass
        link.close()


if __name__ == "__main__":
    main()
