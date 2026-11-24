#!/usr/bin/env python3
"""
Garage Door Opener - Raspberry Pi 3B+ & CC1101 Transceiver
Chamberlain / LiftMaster Security+ 1.0 (Purple Learn Button, 315.00 MHz) Emulation CLI.
"""

import sys
import time
import argparse
import logging

from cc1101 import CC1101
from security_plus import generate_burst_bytes
from remote_manager import RemoteManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("GarageDoor")


def get_radio(bus: int = 0, device: int = 0) -> CC1101:
    """Initialize and configure CC1101 radio."""
    radio = CC1101(bus=bus, device=device)
    radio.init_spi()
    radio.setup_security_plus_315mhz(packet_len=50)
    return radio


def cmd_status(args):
    """Show radio and remote status."""
    manager = RemoteManager(args.config)
    rolling, fixed = manager.peek_codes()

    print("=" * 60)
    print(" Garage Door Opener: Security+ 1.0 (Purple Button 315MHz)")
    print("=" * 60)
    print(f"Remote Name       : {manager.config.get('name')}")
    print(f"Carrier Frequency : {manager.config.get('frequency_mhz')} MHz")
    print(f"Remote Fixed ID   : {fixed} (0x{fixed:X})")
    print(f"Rolling Counter   : {rolling} (0x{rolling:X})")
    print(f"Repeats per Press : {manager.config.get('repeats_per_press', 4)}")
    print(f"Config File       : {manager.config_path}")
    print("-" * 60)

    radio = CC1101(bus=args.spi_bus, device=args.spi_device)
    radio.init_spi()
    if radio.is_mock:
        print("Hardware Status   : SIMULATION MODE (spidev not available or not on Pi)")
    else:
        part = radio.get_partnum()
        ver = radio.get_version()
        state = radio.get_state()
        print(f"CC1101 Hardware   : CONNECTED")
        print(f"Chip Part Number  : 0x{part:02X}")
        print(f"Chip Version      : 0x{ver:02X} (Expected 0x14 or 0x04 for CC1101)")
        print(f"Radio State       : 0x{state:02X} (0x01 = IDLE)")
    print("=" * 60)


def cmd_pair(args):
    """Pair the virtual remote with a Chamberlain opener via the Purple Learn Button."""
    manager = RemoteManager(args.config)
    repeats = args.repeats or 8

    print("=" * 60)
    print(" PAIRING MODE: Chamberlain / LiftMaster Purple Learn Button")
    print("=" * 60)
    print("Step 1: Walk over to your garage door opener ceiling unit.")
    print("Step 2: Locate the PURPLE learn button and press it once firmly.")
    print("        The amber/purple learn indicator LED will turn solid ON.")
    print("        (You have about 30 seconds to transmit the pairing code).")
    print("=" * 60)

    if not args.yes:
        input("Press [Enter] here once the opener learn LED is solid ON...")

    rolling, fixed = manager.get_next_codes()
    print(f"Transmitting pairing signal with Fixed ID {fixed}, Rolling Counter {rolling}...")

    burst = generate_burst_bytes(rolling, fixed)
    radio = get_radio(args.spi_bus, args.spi_device)

    try:
        radio.transmit_security_plus([burst] * repeats, inter_burst_delay=0.03)
    finally:
        radio.close()

    print("\nPairing transmission complete!")
    print("Check your garage door opener:")
    print(" -> If the opener light bulb blinks twice (or you hear two clicks),")
    print("    the remote has been SUCCESSFULLY LEARNED!")
    print(" -> You can now test it with: python garage_door.py toggle")
    print("=" * 60)


def cmd_toggle(args):
    """Transmit open / close command and increment rolling code counter."""
    manager = RemoteManager(args.config)
    repeats = args.repeats or manager.config.get("repeats_per_press", 8)

    rolling, fixed = manager.get_next_codes()
    print(f"Transmitting Open/Close command...")
    print(f" -> Fixed ID: {fixed} | Rolling Code: {rolling} (Repeats: {repeats})")

    burst = generate_burst_bytes(rolling, fixed)
    radio = get_radio(args.spi_bus, args.spi_device)

    try:
        radio.transmit_security_plus([burst] * repeats, inter_burst_delay=0.02)
    finally:
        radio.close()

    new_rolling, _ = manager.peek_codes()
    print(f"Command transmitted successfully! Next rolling counter: {new_rolling}")


def cmd_set_counter(args):
    """Manually update the rolling code counter."""
    manager = RemoteManager(args.config)
    manager.set_rolling_code(args.counter)
    print(f"Rolling code counter updated to: {args.counter}")


def cmd_set_button(args):
    """Change remote button (0 = Large Main Door, 1 = Left, 2 = Right)."""
    manager = RemoteManager(args.config)
    new_fixed = manager.set_button(args.button)
    print(f"Switched to Button {args.button} (New Fixed ID: {new_fixed})")
    print("Note: You must pair this button with your opener: python3 garage_door.py pair")

def cmd_reset_remote(args):
    """Generate a new virtual remote ID."""
    manager = RemoteManager(args.config)
    new_fixed, new_rolling = manager.reset_remote(start_rolling=args.start_counter)
    print(f"Generated new virtual remote:")
    print(f" -> Fixed ID     : {new_fixed}")
    print(f" -> Rolling Code : {new_rolling}")
    print("Note: You must run 'python garage_door.py pair' to pair this new remote ID with your opener.")


def cmd_test_rf(args):
    """Transmit continuous test bursts or unmodulated carrier for RF verification."""
    radio = get_radio(args.spi_bus, args.spi_device)
    print(f"Transmitting test bursts at 315.00 MHz for {args.duration} seconds...")
    print("Use an SDR (e.g. RTL-SDR, HackRF, GQRX) or frequency counter to monitor 315.00 MHz.")

    # Generate dummy test burst
    burst = generate_burst_bytes(12345, 67890)
    start = time.time()
    count = 0
    try:
        while time.time() - start < args.duration:
            radio.transmit_burst(burst)
            time.sleep(0.02)
            count += 1
        print(f"Sent {count} test bursts successfully.")
    finally:
        radio.close()


def cmd_web(args):
    """Launch localhost Web UI and REST API server."""
    from web_server import run_server
    run_server(host=args.host, port=args.port, config_path=args.config, spi_bus=args.spi_bus, spi_device=args.spi_device)


def main():
    parser = argparse.ArgumentParser(
        description="Chamberlain Security+ 1.0 (Purple Learn Button, 315MHz) Garage Door Opener for Raspberry Pi & CC1101"
    )
    parser.add_argument("--config", default="remote_config.json", help="Path to remote config JSON file")
    parser.add_argument("--spi-bus", type=int, default=0, help="SPI bus number (default: 0)")
    parser.add_argument("--spi-device", type=int, default=0, help="SPI chip select / device (default: 0 = CE0)")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: status
    subparsers.add_parser("status", help="Show remote and CC1101 radio status")

    # Command: pair / learn
    sub_pair = subparsers.add_parser("pair", help="Pair virtual remote with garage opener (press purple learn button first)")
    sub_pair.add_argument("-r", "--repeats", type=int, default=8, help="Number of bursts to transmit during pairing (default: 8)")
    sub_pair.add_argument("-y", "--yes", action="store_true", help="Do not prompt for confirmation before transmitting")

    # Command: toggle / open / close
    sub_toggle = subparsers.add_parser("toggle", help="Send Open/Close command (increments rolling code)")
    sub_toggle.add_argument("-r", "--repeats", type=int, default=None, help="Burst repeats (default: 8)")

    subparsers.add_parser("open", help="Alias for toggle")
    subparsers.add_parser("close", help="Alias for toggle")

    # Command: set-button
    sub_btn = subparsers.add_parser("set-button", help="Switch remote button (0=Large Main Door, 1=Left, 2=Right)")
    sub_btn.add_argument("button", type=int, choices=[0, 1, 2], help="Button number (0=Large Main Door)")

    # Command: set-counter
    sub_set = subparsers.add_parser("set-counter", help="Manually set rolling code counter")
    sub_set.add_argument("counter", type=int, help="New rolling code counter integer value")

    # Command: reset-remote
    sub_reset = subparsers.add_parser("reset-remote", help="Generate a new virtual remote ID")
    sub_reset.add_argument("--start-counter", type=int, default=1000, help="Starting rolling counter (default: 1000)")

    # Command: test-rf
    sub_test = subparsers.add_parser("test-rf", help="Transmit RF test bursts to check signal on SDR")
    sub_test.add_argument("-d", "--duration", type=float, default=5.0, help="Duration in seconds (default: 5.0)")

    # Command: web
    sub_web = subparsers.add_parser("web", help="Start localhost Web UI dashboard")
    sub_web.add_argument("--host", default="0.0.0.0", help="Binding host (default: 0.0.0.0)")
    sub_web.add_argument("-p", "--port", type=int, default=8080, help="HTTP port (default: 8080)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command in ("toggle", "open", "close"):
        cmd_toggle(args)
    elif args.command == "pair":
        cmd_pair(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "set-button":
        cmd_set_button(args)
    elif args.command == "set-counter":
        cmd_set_counter(args)
    elif args.command == "reset-remote":
        cmd_reset_remote(args)
    elif args.command == "test-rf":
        cmd_test_rf(args)
    elif args.command == "web":
        cmd_web(args)


if __name__ == "__main__":
    main()
