#!/usr/bin/env python3
"""Fine-tune: line the cursor up with your iron sights.

    python tools/aim_finetune.py [--port COM7]

Shoot the ring at two clearly different distances; nudges are sent with
"aimcal!=", which applies without writing flash.
"""
import argparse, os, queue, sys, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import aim_fit
from aim_calib import (parse_q, find_gun, SerialSource, FRAME_W, FRAME_H,
                       install_over_serial, aimcal_line)

STEP_PX   = 4.0                 # screen px per nudge, at 1920 wide
LEAD_STEP = 5                   # ms per lead nudge
LEAD_MAX  = 30                  # the ceiling the capture layer clamps to anyway
MIN_SPREAD = 1.25               # quad-span ratio between the two stations
# Ring position as a fraction of the window; drawing and measurement share it.
TARGET = (0.50, 0.40)
SHOT_FRAMES = 12                # frames medianed into one measurement

C_BG, C_FG, C_DIM = "#0d1117", "#e6edf3", "#7d8590"
C_OK, C_WARN, C_BAD = "#39c26e", "#d8a13a", "#d24b4b"


class Tuner:
    """State and arithmetic. No GUI, so it is testable headlessly."""

    def __init__(self, calib):
        self.c0 = dict(calib)          # what calibration produced, untouched
        self.stage = 0                 # 0 = near, 1 = far, 2 = done
        self.off = [np.zeros(2), np.zeros(2)]   # normalised screen nudges
        self.q = [None, None]          # a representative quad per station
        self.measured = [False, False]
        self.span = [None, None]
        self.lead = 0
        self.msg = ""

    # ---- the live preview -------------------------------------------------
    def preview(self):
        """Calibration with the current station's nudge applied as a constant."""
        d = self.off[self.stage] if self.stage < 2 else np.zeros(2)
        c = dict(self.c0)
        c['cx'] = self.c0['cx'] + d[0]
        c['cy'] = self.c0['cy'] + d[1]
        return c

    def nudge(self, dx, dy):
        if self.stage > 1: return
        self.off[self.stage] = self.off[self.stage] + np.array(
            [dx * STEP_PX / 1920.0, dy * STEP_PX / 1200.0])

    def nudge_lead(self, d):
        self.lead = int(min(LEAD_MAX, max(0, self.lead + d * LEAD_STEP)))

    def note_quad(self, q):
        """A shot on the ring measures the offset; the residual is additive."""
        if self.stage > 1: return
        q = np.asarray(q)
        self.q[self.stage] = q
        self.span[self.stage] = aim_fit.quad_span(q)
        r = aim_fit.solve(self.preview(), q, FRAME_W, FRAME_H)
        if r is None:
            self.msg = "could not solve that shot -- are all four LEDs visible?"
            return
        resid = np.array([TARGET[0] - r[0], TARGET[1] - r[1]])
        # reject shots that land a whole screen away: a mis-solve, not a sight offset
        if abs(resid[0]) > 0.5 or abs(resid[1]) > 0.5:
            self.msg = ("that shot solved %.0f, %.0f px from the ring -- too far "
                        "to be a sight offset; shoot the ring itself"
                        % (resid[0]*1920.0, resid[1]*1200.0))
            return
        self.off[self.stage] = self.off[self.stage] + resid
        self.measured[self.stage] = True
        self.msg = ""

    # ---- moving between stations -----------------------------------------
    def spread(self):
        if None in self.span: return None
        lo, hi = sorted(self.span)
        return (hi / lo) if lo > 1e-6 else None

    def can_finish(self):
        """Both stations measured, and far enough apart to be separable."""
        if not (self.measured[0] and self.measured[1]):
            return False, "shoot the ring at both distances first"
        sp = self.spread()
        if sp is None or sp < MIN_SPREAD:
            return False, ("the two positions are too close together "
                           "(%.2fx, need %.2fx) -- the angular and constant "
                           "parts cannot be separated" % (sp or 1.0, MIN_SPREAD))
        return True, ""

    def solve(self):
        ok, why = self.can_finish()
        if not ok:
            self.msg = why
            return None
        near, far = (0, 1) if self.span[0] >= self.span[1] else (1, 0)
        r = aim_fit.split_offset(self.c0, self.q[near], self.off[near],
                                 self.q[far], self.off[far], FRAME_W, FRAME_H)
        if r is None:
            self.msg = "the split was refused (degenerate geometry)"
            return None
        return aim_fit.apply_offset(self.c0, *r)


# ---------------------------------------------------------------------------
def run_gui(src, tuner, on_send):
    import tkinter as tk
    root = tk.Tk()
    root.title("Fine tune")
    root.configure(bg=C_BG)
    root.attributes("-fullscreen", True)
    SW = root.winfo_screenwidth(); SH = root.winfo_screenheight()
    cv = tk.Canvas(root, width=SW, height=SH, bg=C_BG, highlightthickness=0)
    cv.pack(fill="both", expand=True)

    st = {"last_send": 0.0, "dirty": True, "done": None, "quad": None,
          # click feedback
          "flash": None, "flash_t": 0.0, "toast": "", "toast_t": 0.0,
          "prev_q": None, "prev_t": 0.0, "vpx": 0.0, "recent": []}

    def text(x, y, s, size, fill, bold=False, anchor="center"):
        cv.create_text(x, y, text=s, anchor=anchor, fill=fill,
                       font=("DejaVu Sans", int(size), "bold" if bold else "normal"))

    # ---- the buttons, as canvas rectangles we hit-test ourselves ----------
    BW, BH, BY = SW * 0.13, SH * 0.11, SH * 0.03
    gap = SW * 0.012
    total = 6 * BW + 5 * gap
    x0 = (SW - total) / 2.0
    BUTTONS = []
    for i, (lab, act) in enumerate((("◀", ("nudge", -1, 0)),
                                    ("▶", ("nudge", +1, 0)),
                                    ("▲", ("nudge", 0, -1)),
                                    ("▼", ("nudge", 0, +1)),
                                    ("LEAD −", ("lead", -1)),
                                    ("LEAD +", ("lead", +1)))):
        bx = x0 + i * (BW + gap)
        BUTTONS.append(dict(x0=bx, y0=BY, x1=bx + BW, y1=BY + BH, lab=lab, act=act))
    DONE = dict(x0=SW*0.40, y0=SH*0.86, x1=SW*0.60, y1=SH*0.94, lab="DONE", act=("done",))

    def hit(x, y):
        for b in BUTTONS + [DONE]:
            if b["x0"] <= x <= b["x1"] and b["y0"] <= y <= b["y1"]:
                return b
        return None

    def say(msg, b=None):
        st["toast"] = msg; st["toast_t"] = time.time()
        st["flash"] = b; st["flash_t"] = time.time()

    def do(act, b=None):
        if act[0] == "nudge":
            tuner.nudge(act[1], act[2]); st["dirty"] = True
            d = tuner.off[tuner.stage] if tuner.stage < 2 else np.zeros(2)
            say("nudged  ->  %+.0f, %+.0f px" % (d[0]*1920.0, d[1]*1200.0), b)
        elif act[0] == "lead":
            before = tuner.lead
            tuner.nudge_lead(act[1])
            on_send("~cam=lead:%d" % tuner.lead)
            if tuner.lead == before:
                say("lead already at %d ms (range 0-%d)" % (tuner.lead, LEAD_MAX), b)
            else:
                say("lead  %d  ->  %d ms" % (before, tuner.lead), b)
        elif act[0] == "done":
            if tuner.stage == 0:
                ok = tuner.measured[0]
                if not ok:
                    tuner.msg = "shoot the ring here first"
                else:
                    tuner.stage = 1; tuner.msg = ""
                st["dirty"] = True
            elif tuner.stage == 1:
                c = tuner.solve()
                if c is not None:
                    tuner.stage = 2
                    st["done"] = c
                st["dirty"] = True

    def click(ev):
        b = hit(ev.x, ev.y)
        if b: do(b["act"], b)
        elif tuner.stage < 2 and len(st["recent"]) >= 4:
            # a shot that is not on a button IS the measurement; median several
            # frames because one frame carries the full blob noise
            q = np.median(np.array(st["recent"][-SHOT_FRAMES:]), axis=0)
            before = tuner.off[tuner.stage].copy()
            tuner.note_quad(q)
            d = tuner.off[tuner.stage] - before
            if not tuner.msg:
                say("measured  ->  %+.0f, %+.0f px" % (d[0]*1920.0, d[1]*1200.0))
            st["dirty"] = True

    cv.bind("<Button-1>", click)
    root.bind("<Escape>", lambda e: root.destroy())
    root.bind("<Left>",  lambda e: do(("nudge", -1, 0), BUTTONS[0]))
    root.bind("<Right>", lambda e: do(("nudge", +1, 0), BUTTONS[1]))
    root.bind("<Up>",    lambda e: do(("nudge", 0, -1), BUTTONS[2]))
    root.bind("<Down>",  lambda e: do(("nudge", 0, +1), BUTTONS[3]))
    root.bind("<minus>", lambda e: do(("lead", -1), BUTTONS[4]))
    root.bind("<plus>",  lambda e: do(("lead", +1), BUTTONS[5]))
    root.bind("<equal>", lambda e: do(("lead", +1), BUTTONS[5]))
    root.bind("<Return>", lambda e: do(("done",)))

    def push():
        """send the live preview, rate limited -- it is a serial link, not a bus"""
        if not st["dirty"]: return
        now = time.time()
        if now - st["last_send"] < 0.05: return
        st["last_send"] = now; st["dirty"] = False
        on_send("~" + aimcal_line(tuner.preview()).replace("aimcal=", "aimcal!=", 1))

    def draw():
        cv.delete("all")
        lit = (time.time() - st["flash_t"]) < 0.18
        for b in BUTTONS:
            on = lit and st["flash"] is b
            cv.create_rectangle(b["x0"], b["y0"], b["x1"], b["y1"],
                                fill="#2d6a43" if on else "#161b22",
                                outline=C_OK if on else "#30363d", width=3 if on else 2)
            # word labels need a smaller face than the arrows or they overrun the box
            fs = SH*0.035 if len(b["lab"]) <= 2 else SH*0.024
            text((b["x0"]+b["x1"])/2, (b["y0"]+b["y1"])/2, b["lab"], fs, C_FG, bold=True)
        # what the lead is worth right now, from the gun's own motion
        worth = st["vpx"] * (tuner.lead / 1000.0) * 1920.0
        text(SW/2, BY + BH + SH*0.026,
             "lead %d ms   -   worth about %.0f screen px at the speed you are "
             "moving right now" % (tuner.lead, abs(worth)), SH*0.019,
             C_OK if tuner.lead else C_DIM)
        text(SW/2, BY + BH + SH*0.055,
             "raise it while the cursor trails you; stop as soon as it "
             "overshoots on direction reversals", SH*0.016, C_DIM)
        if (time.time() - st["toast_t"]) < 1.6 and st["toast"]:
            text(SW/2, BY + BH + SH*0.090, st["toast"], SH*0.024, C_OK, bold=True)

        if tuner.stage == 2:
            text(SW/2, SH*0.42, "FINE TUNE APPLIED", SH*0.045, C_OK, bold=True)
            text(SW/2, SH*0.50, tuner.msg or "saved to the gun", SH*0.022, C_FG)
            text(SW/2, SH*0.56, "Esc to close", SH*0.018, C_DIM)
            return

        # the target you line your IRON SIGHTS up with
        cx, cy = SW*TARGET[0], SH*TARGET[1]
        r = SH*0.035
        cv.create_oval(cx-r, cy-r, cx+r, cy+r, outline=C_WARN, width=3)
        cv.create_line(cx-r*1.8, cy, cx+r*1.8, cy, fill=C_WARN, width=2)
        cv.create_line(cx, cy-r*1.8, cx, cy+r*1.8, fill=C_WARN, width=2)

        head = ("STATION 1 -- stand where you normally play" if tuner.stage == 0
                else "STATION 2 -- step well back, then do it again")
        text(SW/2, SH*0.53, head, SH*0.030, C_FG, bold=True)
        text(SW/2, SH*0.585,
             "Aim at the ring with your IRON SIGHTS and shoot it. That MEASURES "
             "the offset; the arrows are only for touching it up.", SH*0.019, C_DIM)
        d = tuner.off[tuner.stage]
        text(SW/2, SH*0.645, "offset %+.0f, %+.0f px"
             % (d[0]*1920.0, d[1]*1200.0), SH*0.024,
             C_OK if tuner.measured[tuner.stage] else C_DIM)
        text(SW/2, SH*0.695,
             "measured here -- shoot again to refine" if tuner.measured[tuner.stage]
             else "not measured here yet -- shoot the ring", SH*0.019,
             C_OK if tuner.measured[tuner.stage] else C_WARN)
        sp = tuner.spread()
        if tuner.stage == 1 and sp is not None:
            good = sp >= MIN_SPREAD
            text(SW/2, SH*0.745, "distance change %.2fx  (need %.2fx)" % (sp, MIN_SPREAD),
                 SH*0.020, C_OK if good else C_BAD)
        if tuner.msg:
            text(SW/2, SH*0.79, tuner.msg, SH*0.019, C_BAD)
        cv.create_rectangle(DONE["x0"], DONE["y0"], DONE["x1"], DONE["y1"],
                            fill="#161b22", outline="#30363d", width=2)
        text((DONE["x0"]+DONE["x1"])/2, (DONE["y0"]+DONE["y1"])/2,
             "NEXT" if tuner.stage == 0 else "FINISH", SH*0.026, C_FG, bold=True)

    def tick():
        n = 0
        while n < 200:
            try: line = src.q.get_nowait()
            except queue.Empty: break
            n += 1
            pq = parse_q(line)
            if pq is not None:
                q = pq[0]; st["quad"] = q
                st["recent"].append(np.asarray(q)); del st["recent"][:-SHOT_FRAMES]
                # rough aim-point speed (screen widths/sec) for the "worth" readout:
                # difference SOLVED screen positions, not camera pixels
                now = time.time()
                if st["prev_q"] is not None and now > st["prev_t"]:
                    dt = now - st["prev_t"]
                    if 0.002 < dt < 0.2:
                        a = aim_fit.solve(tuner.preview(), st["prev_q"], FRAME_W, FRAME_H)
                        b = aim_fit.solve(tuner.preview(), q, FRAME_W, FRAME_H)
                        if a and b:
                            v = float(np.hypot(b[0]-a[0], b[1]-a[1])) / dt
                            st["vpx"] = 0.8*st["vpx"] + 0.2*v
                st["prev_q"] = q; st["prev_t"] = now
        push()
        draw()
        root.after(16, tick)

    root.after(16, tick)
    root.mainloop()
    return st["done"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=115200)
    a = ap.parse_args()
    port = a.port or find_gun(a.baud)
    if not port:
        sys.exit("no gun found. Run tools/aim_probe.py for a diagnosis.")
    src = SerialSource(port, a.baud)
    src.start()
    time.sleep(0.6)
    src.replies.clear()
    src.ser.write(b"\n~aimcal?\n")
    time.sleep(1.0)
    cal = None
    for r in src.replies:
        if "cx=" in r:
            g = {}
            for tok in r.replace("AIM:", "").split():
                if "=" in tok:
                    k, _, v = tok.partition("=")
                    try: g[k] = float(v)
                    except ValueError: pass
            if all(k in g for k in ("cx", "cy", "w", "h", "bx", "by")):
                cal = dict(magic=aim_fit.MAGIC, cx=g['cx'], cy=g['cy'], w=g['w'],
                           h=g['h'], bx=g['bx'], by=g['by'],
                           lever=g.get('lever', 0.0), rx=g.get('rx', 0.0),
                           ry=g.get('ry', 0.0))
    if cal is None:
        sys.exit("the gun has no calibration loaded -- run tools/aim_calib.py first.")
    print("current: %s" % aimcal_line(cal))

    def send(line):
        try: src.ser.write(("\n%s\n" % line).encode())
        except Exception: pass

    tuner = Tuner(cal)
    out = run_gui(src, tuner, send)
    if out is None:
        send("~" + aimcal_line(cal))         # put the original back, saved
        print("cancelled; original calibration restored")
        return
    print(install_over_serial(src, aimcal_line(out), out))
    send("~camsave")
    print("lead %d ms saved" % tuner.lead)


if __name__ == "__main__":
    main()
