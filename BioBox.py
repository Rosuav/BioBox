import sys
import time
import subprocess
import socket
import threading

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib


class MainUI(Gtk.Window):
	def __init__(self):
		super().__init__(title="Bio Box")
		self.set_border_width(10)
		
		modules = Gtk.Box()
		self.add(modules)
		vlcmodule = VLC()
		modules.pack_start(vlcmodule, True, True, 0)
		c922module = WebcamFocus()
		modules.pack_start(c922module, True, True, 0)

class Channel(Gtk.Box):
	def __init__(self, name):
		super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
		self.set_size_request(50, 300)
		channelname = Gtk.Label(label=name)
		self.pack_start(channelname, False, False, 0)
		self.slider = Gtk.Adjustment(value=100, lower=0, upper=150, step_increment=1, page_increment=10, page_size=0)
		level = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL, adjustment=self.slider, inverted=True)
		level.add_mark(value=100, position=Gtk.PositionType.LEFT, markup=None)
		level.add_mark(value=100, position=Gtk.PositionType.RIGHT, markup=None)
		self.pack_start(level, True, True, 0)
		spinvalue = Gtk.SpinButton(adjustment=self.slider)
		self.pack_start(spinvalue, False, False, 0)
		self.mute = Gtk.ToggleButton(label="Mute")
		self.pack_start(self.mute, False, False, 0)
		self.slider.connect("value-changed", self.write_value)
		self.mute.connect("toggled", self.muted)
	
	# Fallback functions if subclasses don't provide write_value() or muted()
	def write_value(self, widget):
		value = round(widget.get_value())
		print(value)

	def muted(self, widget):
		mute_state = widget.get_active()
		print("Channel " + "un" * (not mute_state) + "muted")

class VLC(Channel):
	def __init__(self):
		super().__init__(name="VLC")
		threading.Thread(target=self.conn, daemon=True).start()
		self.last_wrote = time.time() # TODO: use time.monotonic()

	def conn(self):
		self.sock = sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		sock.connect(('localhost',4221))
		sock.send(b"volume\r\n")
		buffer = b""
		with sock:
			while True:
				data = sock.recv(1024)
				if not data:
					break
				buffer += data
				while b"\n" in buffer:
					line, buffer = buffer.split(b"\n", 1)
					line = line.rstrip().decode("utf-8")
					attr, value = line.split(":", 1)
					if attr == "volume":
						value = int(value)
						print(value)
						GLib.idle_add(self.update_position, value)
					else:
						print(attr, value)
					# TODO: Respond to "muted" signals

	def write_value(self, widget):
		if time.time() > self.last_wrote + 0.01: # TODO: drop only writes that would result in bounce loop
			value = round(widget.get_value())
			self.sock.send(b"volume %d \r\n" %value)
			print("VLC: ", value)

	def update_position(self, value):
		self.slider.set_value(value)
		self.last_wrote = time.time()

	def muted(self, widget): # TODO: send to VLC (depends on support in TellMeVLC)
		mute_state = widget.get_active()
		print("VLC Mute status:", mute_state)

class WebcamFocus(Channel):
	def __init__(self):
		super().__init__(name="C922 Focus")

	def write_value(self, widget):
		value = round(widget.get_value() / 5) * 5
		if not self.mute_state:
			subprocess.run(["v4l2-ctl", "-d", "/dev/webcam_c922", "-c", "focus_absolute=%d" %value])

	def update_position(self, value):
		self.slider.set_value(value)

	def muted(self, widget):
		self.mute_state = widget.get_active()
		subprocess.run(["v4l2-ctl", "-d", "/dev/webcam_c922", "-c", "focus_auto=%d" %self.mute_state])
		# TODO: When autofocus is unset, set focus_absolute to slider position

if __name__ == "__main__":
	win = MainUI()
	win.connect("destroy", Gtk.main_quit)
	win.show_all()
	Gtk.main()
