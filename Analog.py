import os
import asyncio
import time
import bisect
import collections
import busio
import digitalio
import board
import adafruit_mcp3xxx.mcp3008 as MCP
from adafruit_mcp3xxx.analog_in import AnalogIn
import Motor

# create the spi bus
spi = busio.SPI(clock=board.SCK, MISO=board.MISO, MOSI=board.MOSI)

# create the cs (chip select)
cs = digitalio.DigitalInOut(board.D22)

# create the mcp object
mcp = MCP.MCP3008(spi, cs)

# create an analog input channel on pin 0
chan0 = AnalogIn(mcp, MCP.P0)

print('Raw ADC Value: ', chan0.value)
print('ADC Voltage: ' + str(chan0.voltage) + 'V')

TOLERANCE = 4
pot_min = 511
pot_max = 1023
goal = None
interp_values = [511, 538, 569, 603, 643, 689, 739, 799, 869, 955, 1023] # 0-100% travel values
dead_zone_low = 2
dead_zone_high = 3

async def read_position():
	last_read = 0	# this keeps track of the last potentiometer value
	while True:
		await asyncio.sleep(0.015625)
		# we'll assume that the pot didn't move
		pot_changed = False
		# read the analog pin
		# ADC provides a 16-bit value, but the low 5 bits are always floored,
		# so divide by 64 to get more usable numbers without losing precision.
		pot = chan0.value // 64
		# how much has it changed since the last read?
		pot_adjust = abs(pot - last_read)
		if pot_adjust > TOLERANCE or goal is not None:
			pos = remap_range(pot)
			# save the potentiometer reading for the next loop
			last_read = pot
			yield(pos)

def remap_range(raw):
	# Convert values from ADC to travel distance 0-100%
	list_pos = bisect.bisect(interp_values, raw)
	if list_pos == 0: # Check if in dead zones
		return 0
	elif list_pos == len(interp_values):
		return 100
	interp_scale = interp_values[list_pos] - interp_values[list_pos -1]
	delta = raw - interp_values[list_pos -1]
	pos = delta / interp_scale * 10 + (list_pos -1) * 10
	return pos

def interp_shift():
	# Shift all values 0-90% by delta acquired from bounds_test()
	# Throughout testing, 100% has always been consistent
	global interp_values
	shift_values = []
	test_min = bounds_test()
	interp_delta = test_min - interp_values[0]
	for level in interp_values[:-1]:
		shift_values.append(level + interp_delta)
	shift_values.append(interp_values[-1]) # Append original 100% value at the end
	# Add dead zones
	shift_values[0] += dead_zone_low
	shift_values[-1] -= dead_zone_high
	interp_values = shift_values

def bounds_test():
	# Test the analogue value of 0% travel
	global pot_min
	Motor.sleep(False)
	Motor.backward()
	Motor.speed(100)
	span = collections.deque(maxlen=5)
	try:
		while True:
			span.append((chan0.value // 64))
			if len(span) == span.maxlen:
				if max(span) - min(span) < 2:
					Motor.brake()
					Motor.speed(0)
					test_min = span[-1]
					print("Min:", test_min)
					pot_min = test_min
					return test_min
			time.sleep(0.015625)
	finally:
		motor.sleep(True)

async def read_value():
	global goal
	Motor.sleep(False)
	last_speed = None
	last_dir = None
	goal_completed = 0
	safety = collections.deque([0] * 2, 5)
	try:
		async for pos in read_position():
			if goal is not None:
				safety.append(pos)
				if goal < 0:
					goal = 0
					print("Goal set to 0")
				if goal > 100:
					goal = 100
					print("Goal set to 100")
				if goal > pos:
					dir = Motor.forward
					print("Moving forward")
				elif goal < pos:
					dir = Motor.backward
					print("Moving backward")
				else:
					print(pos, goal)
				dist = abs(pos - goal)
				if dist >= 25:
					speed = 100
					print("Desired speed: 100")
				elif dist >= 1:
					speed = 80
				else:
					speed = 0
					dir = Motor.brake
					goal = None
					goal_completed = time.monotonic()
					safety.append(-1)
				if max(safety) - min(safety) < 0.1: # Guard against getting stuck
					# This does not solve slider fighting, but it should stop the motor wearing out as fast
					print("Safety brakes engaged")
					speed = 0
					dir = Motor.brake
					goal = None
					goal_completed = time.monotonic()
				print(dir.__name__, speed, dist)
				if speed != last_speed:
					Motor.speed(speed)
					last_speed = speed
				if dir is not last_dir:
					dir()
					last_dir = dir
			else:
				if time.monotonic() > goal_completed + 0.15:
					yield(pos)
	finally:
		goal = None
		Motor.sleep(True)


def test_slider():
	Motor.sleep(False)
	Motor.forward()
	Motor.speed(10)
	start = chan0.value
	while chan0.value < 36800:
		print(chan0.value, chan0.value - start)
		start = chan0.value
		time.sleep(1/32)
	print(chan0.value, chan0.value - start)
	Motor.sleep(True)

def time_boundaries_forward():
	# TODO: Seek to bottom first?
	Motor.sleep(False)
	Motor.forward()
	Motor.speed(100)
	start = time.time()
	next = 0
	safety = collections.deque([0] * 10, 15)
	try:
		while True:
			cur = chan0.value // 64
			if cur >= interp_values[next]:
				print("%3d: %4d --> %.3f\x1b[K" % (next * 10, cur, time.time() - start))
				next += 1
				if next >= len(interp_values): break
			else:
				print("%3d: %4d ... %.3f\x1b[K" % (next * 10, cur, time.time() - start))
			safety.append(cur)
			if max(safety) - min(safety) < 2: break # Guard against getting stuck
			time.sleep(1 / 1000)
	finally:
		Motor.sleep(True)

def time_boundaries_backward():
	# TODO: Seek to top first?
	Motor.sleep(False)
	Motor.backward()
	Motor.speed(100)
	start = time.time()
	next = len(interp_values) - 1
	safety = collections.deque([0] * 10, 15)
	try:
		while True:
			cur = chan0.value // 64
			if cur <= interp_values[next]:
				print("%3d: %4d --> %.3f\x1b[K" % (next * 10, cur, time.time() - start))
				next -= 1
				if next < 0: break
			else:
				print("%3d: %4d ... %.3f\x1b[K" % (next * 10, cur, time.time() - start))
			safety.append(cur)
			if max(safety) - min(safety) < 2: break # Guard against getting stuck
			time.sleep(1 / 1000)
	finally:
		Motor.sleep(True)

def print_value():
	last = None
	while True:
		value = chan0.value // 64
		if value != last:
			print(value, end="\x1b[K\r")
			last = value
		time.sleep(0.015625)

if __name__ == "__main__":
	try:
		print_value()
	finally:
		Motor.cleanup()
