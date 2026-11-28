#!/usr/bin/env python3
"""
Halo — smart garage CLI (Raspberry Pi 3B+ & CC1101 transceiver)
Chamberlain / LiftMaster Security+ 1.0 (Purple Learn Button, 315.00 MHz) Emulation CLI.

TX Modes:
  --mode fifo   (default) FIFO packet mode — no extra wires needed.
  --mode async  Async GDO0 mode via pigpio — most reliable, needs GDO0→GPIO24.

Usage:
  python3 garage_door.py status
  python3 garage_door.py pair
  python3 garage_door.py toggle
  python3 garage_door.py toggle --mode async          # use pigpio async mode
  python3 garage_door.py toggle --mode async --gdo0 24
  python3 garage_door.py debug                        # dump encoded bytes for diagnosis
"""

import sys
import time
import argparse
import logging

from cc1101 import CC1101
from security_plus import generate_burst_bytes, generate_burst_bits
from remote_manager import RemoteManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Halo")


def get_radio_fifo(bus: int = 0, device: int = 0) -> CC1101:
    """Initialize CC1101 in FIFO mode."""
    radio = CC1101(bus=bus, device=device)
    radio.init_spi()
    radio.setup_security_plus_315mhz(packet_len=50)
    return radio


def get_radio_async(bus: int = 0, device: int = 0) -> CC1101:
    """Initialize CC1101 in async GDO0 mode."""
    radio = CC1101(bus=bus, device=device)
    radio.init_spi()
    radio.setup_async_ook_315mhz()
    return radio


def cmd_status(args):
    """Show radio and remote status."""
    manager = RemoteManager(args.config)
    rolling, fixed = manager.peek_codes()

    print("=" * 60)
    print(" Halo - Security+ 1.0 (Purple Button 315MHz)")
    print("=" * 60)
    print(f"Remote Name       : {manager.config.get('name')}")
    print(f"Carrier Frequency : {manager.config.get('frequency_mhz')} MHz")
    print(f"Remote Fixed ID   : {fixed} (0x{fixed:X})")
    print(f"Rolling Counter   : {rolling} (0x{rolling:X})")
    print(f"Repeats per Press : {manager.config.get('repeats_per_press', 8)}")
    print(f"Config File       : {manager.config_path}")

    # Fixed code breakdown
    button = fixed % 3
    id0    = (fixed // 3) % 3
    id1    = (fixed // 9) % 3
    remote = fixed // 27
    print("-" * 60)
    print(f"Fixed Code Decode : button={button} id0={id0} id1={id1} serial={remote}")
    if button != 0:
        print(f"  ⚠ WARNING: button={button} — should be 0 (large door button)!")
    if id1 == 0:
        print(f"  ⚠ WARNING: id1=0 — opener will treat this as a KEYPAD, not a remote!")
    if button == 0 and id1 != 0:
        print(f"  ✓ Fixed code looks correct (large door button, handheld remote)")

    print("-" * 60)

    radio = CC1101(bus=args.spi_bus, device=args.spi_device)
    radio.init_spi()
    if radio.is_mock:
        print("Hardware Status   : SIMULATION MODE (spidev not available)")
    else:
        part = radio.get_partnum()
        ver = radio.get_version()
        state = radio.get_state()
        print(f"CC1101 Hardware   : CONNECTED")
        print(f"Chip Part Number  : 0x{part:02X}")
        print(f"Chip Version      : 0x{ver:02X} (Expected 0x14 or 0x04 for CC1101)")
        print(f"Radio State       : 0x{state:02X} (0x01 = IDLE)")
    print("=" * 60)


def cmd_debug(args):
    """
    Print the exact rolling code, fixed code, ternary symbols, and burst bytes
    that WILL be transmitted on the next toggle — without actually transmitting.

    Run this to verify your signal matches known-good Security+ 1.0 captures.
    """
    from security_plus import encode, symbols_to_bits

    manager = RemoteManager(args.config)
    rolling, fixed = manager.peek_codes()

    print("=" * 60)
    print(" Security+ 1.0 Transmission Debug")
    print("=" * 60)
    print(f"Rolling Code : {rolling} (0x{rolling:08X})")
    print(f"Fixed Code   : {fixed} (0x{fixed:X})")
    print(f"Masked roll  : {rolling & 0xFFFFFFFE} (0x{rolling & 0xFFFFFFFE:08X})  [bit-0 masked]")
    print()

    code = encode(rolling, fixed)
    print(f"40 Ternary Symbols:")
    print(f"  P1 (symbols 0-19 ): {code[:20]}")
    print(f"  P2 (symbols 20-39): {code[20:]}")
    print()

    # Full burst
    burst_bytes = generate_burst_bytes(rolling, fixed)
    burst_bits  = generate_burst_bits(rolling, fixed)

    print(f"Burst: {len(burst_bytes)} bytes = {len(burst_bits)} bits")
    print(f"Hex dump (50 bytes):")
    hex_str = burst_bytes.hex()
    for i in range(0, len(hex_str), 32):
        print(f"  {hex_str[i:i+32]}")
    print()

    # Show pulse widths in µs for first packet
    p1_symbols = [0] + code[:20]
    bits = symbols_to_bits(p1_symbols)
    print("Packet 1 pulse stream (µs per pulse, 500µs/bit):")
    pulses = []
    state = bits[0]
    count = 0
    for b in bits:
        if b == state:
            count += 1
        else:
            pulses.append((state, count * 500))
            state = b
            count = 1
    pulses.append((state, count * 500))
    desc = []
    for (s, us) in pulses[:20]:  # first 20 pulses
        desc.append(f"{'H' if s else 'L'}{us}")
    print(f"  {' '.join(desc)} ...")
    print()
    print("Compare with real remote capture to verify signal correctness.")
    print("=" * 60)


def _do_transmit(radio: CC1101, rolling: int, fixed: int, repeats: int,
                 mode: str, gdo0: int, inter_delay: float):
    """Core transmission dispatch (FIFO or async)."""
    if mode == "async":
        burst_bits = generate_burst_bits(rolling, fixed)
        all_bits = [burst_bits] * repeats
        radio.transmit_security_plus_async(
            all_bits, gdo0_gpio=gdo0,
            bit_duration_us=500,
            inter_burst_delay=inter_delay
        )
    else:
        burst_bytes = generate_burst_bytes(rolling, fixed)
        radio.transmit_security_plus([burst_bytes] * repeats, inter_burst_delay=inter_delay)


def cmd_pair(args):
    """Pair the virtual remote with a Chamberlain opener via the Purple Learn Button."""
    manager = RemoteManager(args.config)
    repeats = args.repeats or 20
    mode = getattr(args, 'mode', 'fifo')
    gdo0 = getattr(args, 'gdo0', 24)

    print("=" * 60)
    print(" PAIRING MODE: Chamberlain / LiftMaster Purple Learn Button")
    print("=" * 60)
    print("Step 1: Walk to your garage door opener motor unit.")
    print("Step 2: Press the PURPLE learn button once firmly.")
    print("        The amber/purple learn LED will glow solid for ~30 seconds.")
    print("IMPORTANT: Position the CC1101 antenna within 1-2 feet of the")
    print("           opener's purple/gray wire antenna while pairing!")
    print("=" * 60)

    if not args.yes:
        input("Press [Enter] once the opener learn LED is solid ON...")

    rolling, fixed = manager.get_next_codes()
    print(f"Transmitting pairing signal ({repeats} bursts, Fixed ID {fixed}, Rolling {rolling}, mode={mode})...")

    if mode == "async":
        radio = get_radio_async(args.spi_bus, args.spi_device)
    else:
        radio = get_radio_fifo(args.spi_bus, args.spi_device)

    try:
        _do_transmit(radio, rolling, fixed, repeats, mode, gdo0, inter_delay=0.002)
    finally:
        radio.close()

    print("\nPairing transmission complete!")
    print(" -> If the opener light blinks TWICE (or you hear 2 clicks), pairing succeeded.")
    print(" -> Now test it with: python3 garage_door.py toggle")
    print("=" * 60)


def cmd_toggle(args):
    """Transmit open/close command and increment rolling code counter."""
    manager = RemoteManager(args.config)
    repeats = args.repeats or manager.config.get("repeats_per_press", 12)
    mode = getattr(args, 'mode', 'fifo')
    gdo0 = getattr(args, 'gdo0', 24)

    rolling, fixed = manager.get_next_codes()
    print(f"Transmitting Open/Close command (mode={mode})...")
    print(f" -> Fixed ID: {fixed} | Rolling Code: {rolling} | Repeats: {repeats}")

    if mode == "async":
        radio = get_radio_async(args.spi_bus, args.spi_device)
    else:
        radio = get_radio_fifo(args.spi_bus, args.spi_device)

    try:
        _do_transmit(radio, rolling, fixed, repeats, mode, gdo0, inter_delay=0.002)
    finally:
        radio.close()

    new_rolling, _ = manager.peek_codes()
    print(f"Command transmitted! Next rolling counter: {new_rolling}")


def cmd_light(args):
    """Transmit light on/off command (Button 1) and advance rolling code counter."""
    manager = RemoteManager(args.config)
    repeats = args.repeats or manager.config.get("repeats_per_press", 12)
    mode = getattr(args, 'mode', 'fifo')
    gdo0 = getattr(args, 'gdo0', 24)

    rolling, fixed = manager.get_button_codes(button=1)
    print(f"Transmitting Opener Light command (Button 1, mode={mode})...")
    print(f" -> Fixed ID: {fixed} | Rolling Code: {rolling} | Repeats: {repeats}")

    if mode == "async":
        radio = get_radio_async(args.spi_bus, args.spi_device)
    else:
        radio = get_radio_fifo(args.spi_bus, args.spi_device)

    try:
        _do_transmit(radio, rolling, fixed, repeats, mode, gdo0, inter_delay=0.002)
    finally:
        radio.close()

    new_rolling, _ = manager.peek_codes()
    print(f"Light command transmitted! Next rolling counter: {new_rolling}")


def cmd_pair_light(args):
    """Pair the light button (Button 1) using the Multi-Function Door Control."""
    manager = RemoteManager(args.config)
    repeats = args.repeats or 24
    mode = getattr(args, 'mode', 'fifo')
    gdo0 = getattr(args, 'gdo0', 24)

    print("=" * 60)
    print(" PAIR LIGHT BUTTON: Multi-Function Wall Door Control")
    print("=" * 60)
    print("Step 1: Go to the wall-mounted door control inside your garage.")
    print("Step 2: Press and HOLD the LIGHT button on the wall control.")
    print("Step 3: While holding LIGHT, press and HOLD the LOCK button.")
    print("IMPORTANT: Position CC1101 within 1-2 feet of the opener antenna!")
    print("=" * 60)

    if not args.yes:
        input("Press [Enter] here while holding the wall control buttons...")

    rolling, fixed = manager.get_button_codes(button=1)
    print(f"Transmitting Light pairing signal ({repeats} bursts, Button 1 Fixed ID {fixed}, Rolling {rolling})...")

    if mode == "async":
        radio = get_radio_async(args.spi_bus, args.spi_device)
    else:
        radio = get_radio_fifo(args.spi_bus, args.spi_device)

    try:
        _do_transmit(radio, rolling, fixed, repeats, mode, gdo0, inter_delay=0.002)
    finally:
        radio.close()

    print("\nTransmission complete!")
    print(" -> When the opener lights flash, release all buttons.")
    print(" -> Now test the light with: python3 garage_door.py light")
    print("=" * 60)


def cmd_set_counter(args):
    manager = RemoteManager(args.config)
    manager.set_rolling_code(args.counter)
    print(f"Rolling code counter updated to: {args.counter}")


def cmd_set_button(args):
    manager = RemoteManager(args.config)
    new_fixed = manager.set_button(args.button)
    print(f"Switched to Button {args.button} (New Fixed ID: {new_fixed})")
    print("Note: You must re-pair: python3 garage_door.py pair")


def cmd_reset_remote(args):
    manager = RemoteManager(args.config)
    new_fixed, new_rolling = manager.reset_remote(start_rolling=args.start_counter)
    print(f"Generated new virtual remote:")
    print(f" -> Fixed ID     : {new_fixed}")
    print(f" -> Rolling Code : {new_rolling}")
    print("Run 'python3 garage_door.py pair' to pair this new remote ID.")


def cmd_test_cw(args):
    """Transmit an unmodulated Continuous Wave (CW) carrier at 315.00 MHz to test RF output."""
    radio = CC1101(bus=args.spi_bus, device=args.spi_device)
    radio.init_spi()
    radio.reset()
    radio.strobe(0x36)  # SIDLE

    # Configure unmodulated continuous carrier at 315.00 MHz
    config = {
        0x00: 0x2E,  # IOCFG2: High impedance
        0x02: 0x2E,  # IOCFG0: High impedance
        0x0B: 0x06,  # FSCTRL1
        0x0C: 0x00,  # FSCTRL0
        0x0D: 0x0C,  # FREQ2: 315.000 MHz
        0x0E: 0x1D,  # FREQ1
        0x0F: 0x91,  # FREQ0
        0x12: 0x30,  # MDMCFG2: ASK/OOK, no sync
        0x08: 0x32,  # PKTCTRL0: Asynchronous serial mode
        0x18: 0x18,  # MCSM0: Calibrate on IDLE->TX
        0x22: 0x11,  # FREND0: PA power index 1
        0x23: 0xE9,  # FSCAL3
        0x24: 0x2A,  # FSCAL2
        0x25: 0x00,  # FSCAL1
        0x26: 0x1F,  # FSCAL0
        0x2C: 0x81,  # TEST2
        0x2D: 0x35,  # TEST1
        0x2E: 0x09,  # TEST0
    }
    for reg, val in config.items():
        radio.write_reg(reg, val)

    # PATABLE: both 0 and 1 output maximum +10 dBm (unmodulated carrier)
    radio.write_burst(0x3E, [0xC0, 0xC0])
    radio.strobe(0x35)  # STX

    print(f"Transmitting continuous carrier wave at 315.00 MHz for {args.duration}s...")
    print("Use an RTL-SDR, SDR dongle, or RF power meter to verify transmission.")
    try:
        time.sleep(args.duration)
    finally:
        radio.strobe(0x36)  # SIDLE
        radio.close()
        print("Carrier test finished (radio returned to IDLE).")


def cmd_test_rf(args):
    """Transmit continuous test bursts for RF verification."""
    mode = getattr(args, 'mode', 'fifo')
    gdo0 = getattr(args, 'gdo0', 24)

    if mode == "async":
        radio = get_radio_async(args.spi_bus, args.spi_device)
    else:
        radio = get_radio_fifo(args.spi_bus, args.spi_device)

    print(f"Transmitting test bursts at 315.00 MHz for {args.duration} seconds (mode={mode})...")

    start = time.time()
    count = 0
    try:
        while time.time() - start < args.duration:
            _do_transmit(radio, 12345, 67890 * 27 + 9, 1, mode, gdo0, inter_delay=0.002)
            time.sleep(0.010)
            count += 1
        print(f"Sent {count} test bursts successfully.")
    finally:
        radio.close()


def cmd_web(args):
    """Launch localhost Web UI and REST API server."""
    from web_server import run_server
    run_server(host=args.host, port=args.port, config_path=args.config,
               spi_bus=args.spi_bus, spi_device=args.spi_device,
               tx_mode=getattr(args, 'mode', 'fifo'),
               gdo0_gpio=getattr(args, 'gdo0', 24))


def main():
    parser = argparse.ArgumentParser(
        description="Halo smart garage — Chamberlain Security+ 1.0 (Purple 315MHz) via Raspberry Pi & CC1101"
    )
    parser.add_argument("--config", default="remote_config.json")
    parser.add_argument("--spi-bus", type=int, default=0)
    parser.add_argument("--spi-device", type=int, default=0)
    parser.add_argument(
        "--mode", choices=["fifo", "async"], default="fifo",
        help="TX mode: 'fifo' (default, no GDO0 wire) or 'async' (pigpio, needs GDO0→GPIO24)"
    )
    parser.add_argument(
        "--gdo0", type=int, default=24,
        help="BCM GPIO pin connected to CC1101 GDO0 (only used with --mode async, default: 24)"
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="Show remote and CC1101 radio status")

    subparsers.add_parser("debug", help="Print encoded bytes/bits for the next transmission (no RF)")

    sub_pair = subparsers.add_parser("pair", help="Pair virtual remote (press purple learn button first)")
    sub_pair.add_argument("-r", "--repeats", type=int, default=20)
    sub_pair.add_argument("-y", "--yes", action="store_true")

    sub_toggle = subparsers.add_parser("toggle", help="Send Open/Close command")
    sub_toggle.add_argument("-r", "--repeats", type=int, default=12)

    subparsers.add_parser("open",  help="Alias for toggle")
    subparsers.add_parser("close", help="Alias for toggle")

    sub_light = subparsers.add_parser("light", help="Toggle opener lights (Button 1)")
    sub_light.add_argument("-r", "--repeats", type=int, default=12)

    sub_pair_light = subparsers.add_parser("pair-light", help="Pair light button with wall control")
    sub_pair_light.add_argument("-r", "--repeats", type=int, default=24)
    sub_pair_light.add_argument("-y", "--yes", action="store_true")

    sub_btn = subparsers.add_parser("set-button", help="Switch button (0=large door, 1=light, 2=other)")
    sub_btn.add_argument("button", type=int, choices=[0, 1, 2])

    sub_set = subparsers.add_parser("set-counter", help="Manually set rolling code counter")
    sub_set.add_argument("counter", type=int)

    sub_reset = subparsers.add_parser("reset-remote", help="Generate new virtual remote ID")
    sub_reset.add_argument("--start-counter", type=int, default=1000)

    sub_test = subparsers.add_parser("test-rf", help="Transmit RF test bursts")
    sub_test.add_argument("-d", "--duration", type=float, default=5.0)

    sub_cw = subparsers.add_parser("test-cw", help="Transmit unmodulated 315.00 MHz carrier wave (for SDR/meter testing)")
    sub_cw.add_argument("-d", "--duration", type=float, default=5.0)

    sub_web = subparsers.add_parser("web", help="Start the Halo web app & REST API")
    sub_web.add_argument("--host", default="0.0.0.0")
    sub_web.add_argument("-p", "--port", type=int, default=8080)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "status":       cmd_status,
        "debug":        cmd_debug,
        "pair":         cmd_pair,
        "toggle":       cmd_toggle,
        "open":         cmd_toggle,
        "close":        cmd_toggle,
        "light":        cmd_light,
        "pair-light":   cmd_pair_light,
        "set-button":   cmd_set_button,
        "set-counter":  cmd_set_counter,
        "reset-remote": cmd_reset_remote,
        "test-rf":      cmd_test_rf,
        "test-cw":      cmd_test_cw,
        "web":          cmd_web,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
