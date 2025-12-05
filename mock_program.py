import time
import sys
import random
import signal

def handler(signum, frame):
    print(f"[MOCK] Received signal {signum}. Exiting...")
    sys.exit(0)

signal.signal(signal.SIGTERM, handler)
signal.signal(signal.SIGINT, handler)

print("[MOCK] Starting mock program...")
sys.stdout.flush()

counter = 0
while True:
    print(f"[MOCK] Running... Counter: {counter}")
    sys.stdout.flush()
    
    # Simulate occasional crash (1% chance every second)
    if random.random() < 0.01:
        print("[MOCK] CRASHING NOW!")
        sys.stdout.flush()
        sys.exit(1)
        
    time.sleep(1)
    counter += 1
