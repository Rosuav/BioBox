import os
import time
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
Motor.standby(False)
interp_values = [511, 538, 569, 603, 643, 689, 739, 799, 869, 955, 1023] # 0-100% travel values
dead_zone_low = 2
dead_zone_high = 3

def read_position():
	last_read = 0	# this keeps track of the last potentiometer value
	while True:
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
		time.sleep(0.015625)

def remap_range(raw):
	...

def interp_shift():
	# Shift all values 0-90% by delta acquired from bounds_test()
	# Throughout testing, 100% has always been consistent
	shift_values = []
	test_min = bounds_test()
	interp_delta = test_min - interp_values[0]
	for level in interp_values[:-1]:
		shift_values.append(level + interp_delta)
	shift_values.append(interp_values[-1]) # Append original 100% value at the end
	

def bounds_test():
	# Test the analogue value of 0% travel
	global pot_min
	Motor.backward()
	Motor.speed(100)
	span = collections.deque(maxlen=5)
	while True:
		span.append((chan0.value // 64))
		if len(span) == span.maxlen:
			if max(span) - min(span) < 2:
				Motor.brake()
				Motor.speed(0)
				test_min = span[-1]
				print("Min:", test_min)
				pot_min = test_min
				return(test_min)
		time.sleep(0.015625)

def read_value():
	global goal
	last_speed = None
	last_dir = None
	goal_completed = 0
	for pos in read_position():
		if goal is not None:
			if goal < 0:
				goal = 0
			if goal > 100:
				goal = 100
			dist = abs(pos - goal)
			if dist >= 25:
				speed = 100
			elif dist >= 1:
				speed = 80
			else:
				speed = 0
			if goal > pos:
				dir = Motor.forward
			elif goal < pos:
				dir = Motor.backward
			elif goal == pos:
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

def test_slider():
	Motor.forward()
	Motor.speed(10)
	start = chan0.value
	while chan0.value < 36800:
		print(chan0.value, chan0.value - start)
		start = chan0.value
		time.sleep(1/32)
	print(chan0.value, chan0.value - start)
	Motor.stop()
	Motor.speed(0)

if __name__ == "__main__":
	goal = 75
	try:
		last = None
		while True:
			value = chan0.value
			if value != last:
				print(value, end="\x1b[K\r")
				last = value
			time.sleep(0.015625)
	finally:
		Motor.cleanup()
