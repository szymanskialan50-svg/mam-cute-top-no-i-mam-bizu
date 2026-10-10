"""Audio bridge (Windows).

  phone microphone -> PC : raw PCM from the phone is played into the virtual cable
                           "CABLE Input" (VB-Audio). Apps then pick "CABLE Output" as microphone.
  PC sound -> phone      : WASAPI loopback of the default playback device is sent to the phone,
                           which plays it on its speaker / headphones.

Wire format PC -> phone (after the 1-byte ack / key-frame messages 0x01 / 0x02):
    0x03 [len u16][rate u32][channels u8][PCM s16le]   PC sound
    0x04 [on/off u8]                                   microphone on / off
Wire format phone -> PC: packet type 2: [rate u32][PCM s16le mono]
"""
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile

import numpy as np

try:
    import pyaudiowpatch as pyaudio
except Exception:                                # noqa: BLE001 - not installed / not Windows
    pyaudio = None

VBCABLE_URL = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack43.zip"
VBCABLE_PAGE = "https://vb-audio.com/Cable/"
APPLE_DEVICES_STORE = "ms-windows-store://pdp/?productid=9NP83LWLPZ9K"      # "Apple Devices" app
APPLE_FALLBACK_PAGE = "https://www.apple.com/itunes/"


class AudioBridge:
    def __init__(self):
        self.pa = None
        self.pc_stream = None
        self.pc_rate = 48000
        self.pc_ch = 2
        self.send = None
        self.mic_q = queue.Queue(maxsize=30)
        self.mic_thread = None
        self.halt = False
        self.last_error = ""

    # ---- helpers ------------------------------------------------------------------------
    def available(self):
        return pyaudio is not None and sys.platform == "win32"

    def _pa(self):
        if self.pa is None and pyaudio is not None:
            self.pa = pyaudio.PyAudio()
        return self.pa

    def refresh_devices(self):
        """PortAudio caches the device list: re-create it (e.g. after installing the cable)."""
        if self.pa is not None:
            try:
                self.pa.terminate()
            except Exception:                    # noqa: BLE001
                pass
            self.pa = None

    def cable_index(self):
        """Index of 'CABLE Input' (MME host API: Windows converts any sample rate for us)."""
        pa = self._pa()
        if pa is None:
            return None
        try:
            mme = pa.get_host_api_info_by_type(pyaudio.paMME)["index"]
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                if d["hostApi"] == mme and d["maxOutputChannels"] > 0 and "CABLE Input" in d["name"]:
                    return i
        except Exception as e:                   # noqa: BLE001
            self.last_error = str(e)
        return None

    # ---- PC sound -> phone --------------------------------------------------------------
    def start_pc(self, send):
        self.send = send
        if self.pc_stream is not None:
            return True
        pa = self._pa()
        if pa is None:
            return False
        try:
            dev = pa.get_default_wasapi_loopback()
            self.pc_rate = int(dev["defaultSampleRate"])
            self.pc_ch = max(1, int(dev["maxInputChannels"]))
            self.pc_stream = pa.open(format=pyaudio.paInt16, channels=self.pc_ch, rate=self.pc_rate,
                                     frames_per_buffer=max(256, self.pc_rate // 50), input=True,
                                     input_device_index=dev["index"], stream_callback=self._pc_cb)
            return True
        except Exception as e:                   # noqa: BLE001
            self.last_error = str(e)
            self.pc_stream = None
            return False

    def _pc_cb(self, data, frames, info, status):
        send = self.send
        if send is not None:
            try:
                ch = self.pc_ch
                if ch > 2:                       # 5.1 / 7.1 -> front left/right only
                    data = np.frombuffer(data, np.int16).reshape(-1, ch)[:, :2].tobytes()
                    ch = 2
                send(struct.pack(">BHIB", 3, 5 + len(data), self.pc_rate, ch) + data)
            except Exception:                    # noqa: BLE001
                pass
        return (None, pyaudio.paContinue)

    def stop_pc(self):
        s, self.pc_stream = self.pc_stream, None
        if s is not None:
            try:
                s.stop_stream()
                s.close()
            except Exception:                    # noqa: BLE001
                pass

    # ---- phone microphone -> PC (virtual cable) ------------------------------------------
    def mic_write(self, rate, pcm):
        if self.mic_thread is None:
            self.mic_thread = threading.Thread(target=self._mic_loop, daemon=True)
            self.mic_thread.start()
        try:
            self.mic_q.put_nowait((rate, pcm))
        except queue.Full:
            pass                                 # PC clock slower than the phone: drop

    def _mic_loop(self):
        stream = None
        cur = None
        while not self.halt:
            try:
                rate, pcm = self.mic_q.get(timeout=0.3)
            except queue.Empty:
                continue
            if stream is None or cur != rate:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:            # noqa: BLE001
                        pass
                    stream = None
                idx = self.cable_index()
                if idx is None:
                    continue
                try:
                    stream = self._pa().open(format=pyaudio.paInt16, channels=1, rate=int(rate),
                                             output=True, output_device_index=idx)
                    cur = rate
                except Exception as e:           # noqa: BLE001
                    self.last_error = str(e)
                    stream = None
                    continue
            try:
                stream.write(pcm)
            except Exception:                    # noqa: BLE001
                stream = None

    def close(self):
        self.halt = True
        self.stop_pc()
        self.refresh_devices()


# ---- driver helpers -----------------------------------------------------------------------
def open_apple_driver_page():
    """Apple USB driver (needed for the USB cable mode): opens the Microsoft Store page."""
    try:
        os.startfile(APPLE_DEVICES_STORE)        # noqa: S606 - Windows only
    except Exception:                            # noqa: BLE001
        webbrowser.open(APPLE_FALLBACK_PAGE)


def install_vbcable(say):
    """Downloads the official VB-CABLE driver pack and starts its installer (one UAC prompt)."""
    import urllib.request
    try:
        say("Downloading VB-CABLE from vb-audio.com ...")
        tmp = tempfile.mkdtemp(prefix="usccam_vbcable_")
        zpath = os.path.join(tmp, "vbcable.zip")
        with urllib.request.urlopen(VBCABLE_URL, timeout=40) as r, open(zpath, "wb") as f:
            f.write(r.read())
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp)
        setup = None
        for root, _dirs, files in os.walk(tmp):
            for name in files:
                if name.lower() == "vbcable_setup_x64.exe":
                    setup = os.path.join(root, name)
        if setup is None:
            raise RuntimeError("installer not found in the download")
        say("Installing VB-CABLE - accept the Windows prompt ...")
        import ctypes
        ctypes.windll.shell32.ShellExecuteW(None, "runas", setup, "-i -h", os.path.dirname(setup), 0)
        time.sleep(8)
        say("VB-CABLE installed (restart the app / Windows if the microphone does not show up).")
        return True
    except Exception:                            # noqa: BLE001
        say("Automatic download failed - opening vb-audio.com/Cable, install it manually.")
        webbrowser.open(VBCABLE_PAGE)
        return False
