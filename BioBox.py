import os
import sys
import time
import subprocess
import asyncio
from asyncio import create_task
import WebSocket # Local library for connecting to browser extension
import websockets # ImportError? pip install websockets
import json
import hashlib
import base64


import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

import gbulb
gbulb.install(gtk=True)

try:
	import Analog
	from Motor import cleanup as motor_cleanup
except (ImportError, NotImplementedError, RuntimeError): # Provide a dummy for testing
	def motor_cleanup():
		pass
	class Analog():
		goal = None
		next_goal_time = 0
		async def read_value():
			yield 0 # Yield once and then stop
			# Just as a function is destined to yield once, and then face termination...
			# TODO: instead of creating dummy function, disable slider task on startup

import config # ImportError? See config_example.py

selected_channel = None
webcams = {}
tabs = {}
sites = {
	"music.youtube.com": "YT Music",
	"www.youtube.com": "YouTube",
	"www.twitch.tv": "Twitch",
	"clips.twitch.tv": "Twitch Clips",
	"": "Browser: File",
}
# YouTube only gives a "normalised" value which is different per video. Raising
# the volume above this value has no aural effect in YouTube but is accepted by
# the page. With no way to get the raw volume or the max normalised volume, it
# is impossible to rescale the value to match a 0-100 scale, so the best we can
# do is to use what we have as is.
# Twitch VODs and clips viewed at www.twitch.tv/videos/[id] are uncontrollable.
# The extension runs as expected but never gets a volumechange event. Control
# still works (though in-player slider does not respond) on livestreams and
# clips viewed on clips.twitch.tv.
obs_sources = {}
source_types = ['browser_source', 'pulse_input_capture', 'pulse_output_capture']
# TODO: Configure OBS modules within BioBox

UI_HEADER = """
<ui>
	<menubar name='MenuBar'>
		<menu action='ModulesMenu'>
"""
UI_FOOTER = """
		</menu>
	</menubar>
</ui>
"""

def report(msg):
	print(time.time(), msg)

# Slider
async def read_analog():
	# Get analog value from Analog.py and write to selected channel's slider
	async for pos in Analog.read_value():
		if selected_channel:
			print("From slider:", pos)
			# So far I have no reason for a module with a non-zero minimum
			scale_max = selected_channel.max
			# Scale 0-1023 to scale_max
			value = pos * scale_max / 1023
			selected_channel.refract_value(value, "analog")
			Analog.next_goal_time = time.monotonic() + 0.15

def init_motor_pos(): # TODO: Revisit selecting a module on startup
	if selected_channel:
		scale_max = selected_channel.max
		Analog.goal = selected_channel.slider.get_value() / scale_max * 1023
	else:
		Analog.goal = 1023

# VLC
async def vlc():
	vlc_module = None
	try:
		reader, writer = await asyncio.open_connection(config.host, config.vlc_port)
		writer.write(b"volume\r\nmuted\r\n") # Ask volume and mute state
		await writer.drain()
		vlc_module = VLC(writer)
		await vlc_buf_read(vlc_module, reader)
	except ConnectionRefusedError:
		print("Could not connect to VLC on %s:%s - is TMV running?" % (config.host, config.vlc_port))
	finally:
		if vlc_module:
			vlc_module.remove()
			writer.close() # Close connection and remove module
			await writer.wait_closed()
		print("VLC cleanup done")

async def vlc_buf_read(vlc_module, reader):
	while True:
		data = await reader.readline()
		if not data:
			break
		line = data.decode("utf-8")
		attr, value = line.split(":", 1)
		if attr == "volume":
			vlc_module.refract_value(float(value), "backend")
		elif attr == "muted":
			vlc_module.mute.set_active(int(value))
		else:
			print("From VLC:", attr, value)

# Webcam
async def webcam():
	ssh = None
	async def cleanup():
		ssh.stdin.write(b"quit foo\n")
		try:
			await asyncio.wait_for(ssh.stdin.drain(), timeout=5)
		except asyncio.TimeoutError:
			ssh.terminate()
	try:
		# Begin cancellable section
		ssh = await asyncio.create_subprocess_exec("ssh", "-oBatchMode=yes", (config.webcam_user + "@" + config.host), "python3", config.webcam_control_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
		while True:
			try:
				done, pending = await asyncio.wait([create_task(ssh.stdout.readline()), create_task(ssh.wait())], return_when=asyncio.FIRST_COMPLETED)
			except ConnectionResetError:
				print("SSH connection lost")
				break
			if ssh.returncode is not None:
				break
			try:
				data = next(iter(done)).result()
			except BaseException as e:
				print(type(e))
				print(e)
				break
			line = data.decode("utf-8")
			device, sep, attr = line.rstrip().partition(": ")
			if sep:
				if device == "Unknown command":
					print(line)
				elif device == "Info":
					if attr == "Hi":
						for cam_name, cam_path in config.webcams.items():
							webcams[cam_path] = WebcamFocus(cam_name, cam_path, ssh)
						await ssh.stdin.drain()
					elif attr == "Bye":
						print("camera.py quit")
						break
					else:
						print(line)
				else:
					cmd, sep, value = attr.partition(": ")
					if not sep:
						continue
					if cmd == "set_range":
						min, max, step = map(int, value.split())
						webcams[device].min = min
						webcams[device].max = max
						webcams[device].slider.set_lower(webcams[device].min)
						webcams[device].slider.set_upper(webcams[device].max)
						webcams[device].slider.set_page_increment(step)
					elif cmd == "focus_absolute":
						webcams[device].refract_value(int(value), "backend")
					elif cmd == "focus_auto":
						webcams[device].mute.set_active(int(value))
					elif cmd == "Error" and value == "Device not found":
						print("Device not found:", device)
						webcams[device].remove()
					elif cmd == "Error":
						print("Received error on %s: " %device, value)
	finally:
		for cam in list(webcams):
			webcams[cam].remove()
		print("Done removing webcams")
		await cleanup()
		print("SSH cleanup done")
		ssh = None

# OBS
async def obs_ws():
	obs_uri = "ws://%s:%d" % (config.host, config.obs_port)
	# TODO: Support obs-websocket v5 - coming in OBS 28
	global obs
	auth_key = ""
	rpc_version = 1
	try:
		# Begin cancellable section
		async with websockets.connect(obs_uri) as obs:
			while True:
				data = await obs.recv()
				msg = json.loads(data)
				collector = {}
				if msg.get("op") == 0: # Hello
					if msg.get("d")["rpcVersion"] != rpc_version: # Warn if RPC version is ever bumped
						print("Warning: OBS-Websocket version", msg.get("d")["obsWebSocketVersion"], "has RPC version", msg.get("d")["rpcVersion"])
					if msg.get("d")["authentication"]:
						challenge = msg.get("d")["authentication"]["challenge"].encode("utf-8")
						salt = msg.get("d")["authentication"]["salt"].encode("utf-8")
						auth_key = base64.b64encode(hashlib.sha256(base64.b64encode(hashlib.sha256(config.obs_password + salt).digest()) + challenge).digest())
					ident = {"op": 1, "d": {"rpcVersion": rpc_version, "authentication": auth_key.decode("utf-8"), "eventSubscriptions": 13}}
					# Subscriptions: General (1), Scenes (4), Inputs (8)
					await obs.send(json.dumps(ident))
				elif msg.get("op") == 2: # Identified
					if msg.get("d")["negotiatedRpcVersion"] != rpc_version: # Warn if RPC version is ever bumped
						print("Warning: negotiated RPC version:", msg.get("d")["rpcVersion"])
					await obs.send(json.dumps({"op": 6, "d": {"requestType": "GetCurrentProgramScene", "requestId": "init"}}))
					# Now needs to become GetCurrentProgramScene followed by GetSceneItemList
				elif msg.get("op") == 5: # Event
					if msg.get("d")["eventType"] == "SourceVolumeChanged":
						obs_sources[msg["sourceName"]].refract_value(max(msg["volume"], 0) ** 0.5 * 100, "backend")
					elif msg.get("d")["eventType"] == "SourceMuteStateChanged":
						obs_sources[msg["sourceName"]].mute.set_active(msg["muted"])
					elif msg.get("d")["eventType"] == "CurrentProgramSceneChanged":
						print(msg["d"]["eventData"]["sceneName"])
						list_scene_sources(msg['sources'], collector) # Now need separate request GetSceneItemList
						for source in list(obs_sources):
							if source not in collector:
								print("Removing", source)
								obs_sources[source].remove()
								obs_sources.pop(source, None)
				elif msg.get("op") == 7: # RequestResponse
					if msg.get("d")["requestId"] == "init":
						scene_name = msg.get("d")["responseData"]["currentProgramSceneName"]
						await obs.send(json.dumps({"op": 6, "d": {"requestType": "GetSceneItemList", "requestId": "init2", "requestData": {"sceneName": scene_name}}}))
					elif msg.get("d")["requestId"] == "init2":
						obs_sources.clear()
						sources = msg["d"]["responseData"]["sceneItems"]
						sources_request = {"op": 8, "d": {"requestId": scene_name, "requests": []}}
						for source in sources:
							if source['inputKind'] in source_types:
								sources_request["d"]["requests"].append({"requestType": "GetInputVolume", "requestId": source["sourceName"], "requestData": {"inputName": source["sourceName"]}})
								sources_request["d"]["requests"].append({"requestType": "GetInputMute", "requestId": source["sourceName"], "requestData": {"inputName": source["sourceName"]}})
						await obs.send(json.dumps(sources_request))
				elif msg.get("op") == 9: # RequestBatchResponse
					if msg["d"]["requestId"] == scene_name:
						for response in msg["d"]["results"]:
							# Get volume then mute state per source and add as attributes to source
						# Once each source has its volume and mute state:
							if source['name'] not in obs_sources:
								obs_sources[source['name']] = OBS(source)

	except websockets.exceptions.ConnectionClosedOK:
		pass # Context manager plus finally section should clean everything up, just catch the exception
	except OSError as e:
		if e.errno != 111: raise
		# Ignore connection-refused and just let the module get cleaned up
	finally:
		for source in obs_sources.values():
			source.remove()
		obs_sources.clear()
		print("OBS cleanup done")

def obs_send(request):
	asyncio.run_coroutine_threadsafe(obs.send(json.dumps(request)), loop)

def list_scene_sources(sources, collector):
	for source in sources:
		if source['inputKind'] in source_types:
			#await obs.send(json.dumps({"op": 6, "d": {}))
			print(source['sourceName'], source['volume'], "Muted:", source['muted'])
			collector[source['name']] = source
			if source['name'] not in obs_sources:
				obs_sources[source['name']] = OBS(source)
		#elif source['type'] == 'group':
		#	list_scene_sources(source['groupChildren'], collector)
		# Groups are fiddly and/or broken
		#elif source['type'] == 'scene':
		#	pass
		# Scenes and groups may end up being done the same way

# Browser
def new_tab(tabid, host):
	if host in sites:
		tabname = sites[host]
	else:
		tabname = host
	print("Creating channel for new tab:", tabid)
	newtab = Browser(tabid, tabname)
	tabs[tabid] = newtab

def closed_tab(tabid):
	print("Destroying channel for closed tab:", tabid)
	tabs[tabid].remove()
	tabs.pop(tabid, None)

def tab_volume_changed(tabid, volume, mute_state):
	print("On", tabid, ": Volume:", volume, "Muted:", bool(mute_state))
	channel = tabs[tabid]
	channel.refract_value(float(volume * 100), "backend")
	channel.mute.set_active(int(mute_state))

class Channel(Gtk.Frame):
	mute_labels = ("Mute", "Muted")
	step = 0.01
	max = 150
	min = 0
	channel_types = []

	def __init_subclass__(cls, **kwargs):
		# This ensures that subclasses defined elsewhere are counted for menus
		cls.channel_types.append(cls)
		super().__init_subclass__(**kwargs)

	def __init__(self, name):
		super().__init__(label=name, shadow_type=Gtk.ShadowType.ETCHED_IN)
		super().set_label_align(0.5,0)
		# Box stuff
		box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
		box.set_size_request(50, 300) #TODO: Optimize size and widget scaling for tablet
		self.add(box)
		self.channel_name = name
		# Slider stuff
		self.oldvalue = 100.0
		self.slider = Gtk.Adjustment(value=self.oldvalue, lower=self.min, upper=self.max, step_increment=1.0, page_increment=1.0, page_size=0)
		level = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL, adjustment=self.slider, inverted=True, draw_value=False)
		level.add_mark(value=100, position=Gtk.PositionType.LEFT, markup=None)
		level.add_mark(value=100, position=Gtk.PositionType.RIGHT, markup=None)
		box.pack_start(level, True, True, 0)
		level.connect("focus", self.focus_delay)
		self.slider_signal = self.slider.connect("value-changed", self.adjustment_changed)
		# Spinner
		spinvalue = Gtk.SpinButton(adjustment=self.slider, digits=2)
		box.pack_start(spinvalue, False, False, 0)
		spinvalue.connect("focus", self.focus_delay) # TODO: get signal for +/- presses
		# Mute button
		self.mute = Gtk.ToggleButton(label=self.mute_labels[0])
		box.pack_start(self.mute, False, False, 0)
		self.mute.connect("toggled", self.muted)
		self.mute.connect("focus", self.focus_delay)
		# Channel selector
		self.selector = Gtk.RadioButton.new_from_widget(chan_select)
		self.selector.set_label("Selected")
		box.pack_start(self.selector, False, False, 0)
		self.selector.connect("toggled", self.check_selected)
		self.connect("event", self.click_anywhere)
		# Add self to group
		self.group.pack_start(self, True, True, 0)
		self.group.show_all()

	def focus_delay(self, widget, direction):
		GLib.idle_add(self.focus_select, widget)

	def focus_select(self, widget):
		# Select a module if it gains focus
		# This will also select the first module on startup as its scale
		# will be the first object and will be given focus initially.
		if widget.is_focus():
			self.selector.set_active(True)
			print(self.channel_name, "pulled focus")

	def click_anywhere(self, widget, event):
		if "BUTTON" in event.get_event_type().value_name:
			# TODO: Get scroll wheel changing Gtk.Scale
			self.selector.set_active(True)
			return False
		elif event.get_event_type().value_name != "GDK_MOTION_NOTIFY":
			print(event.get_event_type().value_name)

	def check_selected(self, widget):
		global selected_channel
		if widget.get_active():
			selected_channel = self
			print(selected_channel.channel_name, "selected")
			self.write_analog(selected_channel.slider.get_value())

	def adjustment_changed(self, widget):
		value = widget.get_value()
		self.refract_value(value, "gtk")

	def refract_value(self, value, source):
		# Send value to multiple places, keeping track of sent value to
		# avoid bounce or slider fighting.
		if abs(value - self.oldvalue) > 1: # Prevent feedback loop when moving slider
			#print(self.channel_name, source, value)
			if source != "gtk":
				self.update_position(value)
			if source != "analog":
				if selected_channel is self:
					self.write_analog(value)
			if source != "backend":
				self.write_external(value)
			self.oldvalue = value

	def write_analog(self, value):
		Analog.next_goal = value / self.max * 1023
		print("Slider goal: %s" % Analog.next_goal)

	# Fallback function if subclasses don't provide write_external()
	def write_external(self, value):
		print(self.channel_name, value)

	# Fallback/superclass functions
	def muted(self, widget):
		mute_state = widget.get_active()
		self.mute.set_label(self.mute_labels[mute_state])
		print(self.channel_name, "un" * (not mute_state) + "muted")
		return mute_state

	def update_position(self, value):
		with self.slider.handler_block(self.slider_signal):
			self.slider.set_value(value)

	def remove(self):
		global selected_channel
		if selected_channel is self:
			selected_channel = None # Because it doesn't make sense to select another module
		print("Removing:", self.channel_name)
		self.group.remove(self)

import builtins; builtins.Channel = Channel; import obs

class VLC(Channel):
	group_name = "VLC"
	step = 1.0

	def __init__(self, writer):
		super().__init__(name="VLC")
		self.writer = writer

	def write_external(self, value):
		self.writer.write(b"volume %d \r\n" %value)
		asyncio.create_task(self.writer.drain())
		print("To VLC: ", value)

	def muted(self, widget):
		mute_state = super().muted(widget)
		self.writer.write(b"muted %d \r\n" %mute_state)
		asyncio.create_task(self.writer.drain())
		print("VLC Mute status:", mute_state)

class WebcamFocus(Channel):
	group_name = "Webcams"
	mute_labels = ("AF Off", "AF On")
	step = 1.0 # Cameras have different steps but v4l2 will round any value to the step for the camera in question

	def __init__(self, cam_name, cam_path, ssh):
		self.device_name = cam_name
		super().__init__(name=self.device_name)
		self.device = cam_path
		self.ssh = ssh
		self.ssh.stdin.write(("cam_check %s \n" %self.device).encode("utf-8"))
		# Drain done at backend after starting all webcam modules

	def write_external(self, value):
		# v4l2-ctl throws an error if focus_absolute is changed while AF is on.
		# Therefore, if AF is on, quietly do nothing.
		# Feedback continues when AF is on, so theoretically value should be correct.
		if not self.mute.get_active():
			self.ssh.stdin.write(("focus_absolute %d %s\n" % (value, self.device)).encode("utf-8"))
			asyncio.create_task(self.write_ssh())

	async def write_ssh(self):
		try:
			await self.ssh.stdin.drain()
		except ConnectionResetError as e:
			print("SSH connection lost")

	def muted(self, widget):
		mute_state = super().muted(widget)
		self.ssh.stdin.write(("focus_auto %d %s\n" % (mute_state, self.device)).encode("utf-8"))
		asyncio.create_task(self.ssh.stdin.drain())
		print("%s Autofocus " %self.device_name + ("Dis", "En")[mute_state] + "abled")

class OBS(Channel):
	group_name = "OBS (deprecated)"
	
	def __init__(self, source):
		self.name = source['name']
		super().__init__(name=self.name)
		self.refract_value(max(source['volume'], 0) ** 0.5 * 100, "backend")
		self.mute.set_active(source['muted'])

	def write_external(self, value):
		obs_send({"request-type": "SetVolume", "message-id": "volume", "source": self.name, "volume": ((value / 100) ** 2)})

	def muted(self, widget):
		mute_state = super().muted(widget)
		obs_send({"request-type": "SetMute", "message-id": "mute", "source": self.name, "mute": mute_state})

class Browser(Channel):
	group_name = "Browser"
	
	def __init__(self, tabid, tabname):
		super().__init__(name=tabname)
		self.tabid = tabid

	def write_external(self, value):
		asyncio.create_task(WebSocket.set_volume(self.tabid, (value / 100)))
	
	def muted(self, widget):
		mute_state = super().muted(widget)
		asyncio.create_task(WebSocket.set_muted(self.tabid, mute_state))

async def main():
	stop = asyncio.Event() # Hold open until destroy signal triggers this event
	main_ui = Gtk.Window(title="Bio Box")
	main_ui.set_resizable(False)
	action_group = Gtk.ActionGroup(name="biobox_actions")

	menubox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
	main_ui.add(menubox)
	modules = Gtk.Box()
	modules.set_border_width(10)
	global chan_select
	chan_select = Gtk.RadioButton()
	ui_items = ""
	menu_entries = []
	class Task():
		running = {}
		def VLC():
			return vlc()
		def WebcamFocus():
			return webcam()
		def OBS():
			return obs.obs_ws()
		def Browser():
			return WebSocket.listen(connected=new_tab, disconnected=closed_tab, volumechanged=tab_volume_changed)
	def toggle_menu_item(widget):
		toggle_group = widget.get_name()
		if widget.get_active():
			start_task(toggle_group)
		else:
			asyncio.create_task(cancel_task(toggle_group))
	def start_task(task):
		obj = asyncio.create_task(getattr(Task, task)())
		Task.running[task] = obj
	async def cancel_task(task):
		t = Task.running.pop(task)
		print("Cancelling", task)
		t.cancel()
		print(task, "cancelled")
		try:
			await t
		except asyncio.CancelledError:
			pass
		finally:
			print(task, "cancellation complete")
	async def cancel_all():
		print("Shutting down - cancelling all tasks")
		await asyncio.gather(*[cancel_task(t) for t in Task.running])
		print("All tasks cancelled")
		stop.set()
	for category in Channel.__subclasses__():
		category_ref = category.__name__
		group_name = category.group_name
		group = Gtk.Box(name=group_name)
		category.group = group
		modules.add(group)
		menuitem = "<menuitem action='%s' />" %category_ref
		ui_items += menuitem
		menu_entry = (category_ref, None, group_name, None, None, toggle_menu_item, True)
			    # Action name   ID	  Label	      Accel Tooltip Callback func   Default state
		menu_entries.append(menu_entry)
	ui_tree = UI_HEADER + ui_items + UI_FOOTER
	action_group.add_action(Gtk.Action(name="ModulesMenu", label="Modules"))
	action_group.add_toggle_actions(menu_entries)
	ui_manager = Gtk.UIManager()
	ui_manager.add_ui_from_string(ui_tree)
	ui_manager.insert_action_group(action_group)
	menubar = ui_manager.get_widget("/MenuBar")
	menubox.pack_start(menubar, False, False, 0)
	menubox.add(modules)


	GLib.timeout_add(1000, init_motor_pos)
	# Show window
	def halt(*a): # We could use a lambda function unless we need IIDPIO
		asyncio.create_task(cancel_all())
	main_ui.connect("destroy", halt)
	main_ui.show_all()
	slider_task = asyncio.create_task(read_analog())
	start_task("VLC")
	start_task("OBS")
	start_task("Browser")
	start_task("WebcamFocus")
	await stop.wait()
	motor_cleanup()
	
if __name__ == "__main__":
	css = b"""
		window {-gtk-dpi: 90;}
		scale slider {
			background-size: 20px 40px;
			min-width: 20px;
			min-height: 40px;
		}
	"""
	# TODO: Make this look good without hard-coding
	style_provider = Gtk.CssProvider()
	style_provider.load_from_data(css)
	Gtk.StyleContext.add_provider_for_screen(
		Gdk.Screen.get_default(),
		style_provider,
		Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
	)

	loop = asyncio.new_event_loop()
	asyncio.set_event_loop(loop)
	loop.run_until_complete(main())
