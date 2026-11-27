"""
TI CC1101 SPI Transceiver Driver for Raspberry Pi.
Configured for 315.00 MHz ASK/OOK Transmission.

Supports two TX modes:
  1. FIFO Mode (default):  Writes OOK bit-stream bytes to CC1101 TX FIFO.
                           Works without GDO0 wired. Fast but subject to
                           CC1101 packet-engine framing.
  2. Pigpio Async Mode:    Bit-bangs the OOK pulse train directly into GDO0
                           using pigpio DMA waveforms for microsecond accuracy.
                           Requires GDO0 wired to a Pi GPIO pin.
                           This is the most reliable mode for Security+ 1.0.

Usage:
    radio = CC1101()
    radio.init_spi()

    # FIFO mode (no GDO0 wire needed):
    radio.setup_security_plus_315mhz()
    radio.transmit_burst(burst_bytes)

    # Async/pigpio mode (requires GDO0 → GPIO24):
    radio.setup_async_ook_315mhz()
    radio.transmit_async_bits(burst_bits, gdo0_gpio=24)
"""

import time
import logging
from typing import List

logger = logging.getLogger(__name__)


class RadioError(Exception):
    """The CC1101 didn't do what was asked (not answering, or a transmission didn't go out)."""

# ---------------------------------------------------------------------------
# CC1101 Configuration Registers
# ---------------------------------------------------------------------------
REG_IOCFG2   = 0x00
REG_IOCFG1   = 0x01
REG_IOCFG0   = 0x02
REG_FIFOTHR  = 0x03
REG_SYNC1    = 0x04
REG_SYNC0    = 0x05
REG_PKTLEN   = 0x06
REG_PKTCTRL1 = 0x07
REG_PKTCTRL0 = 0x08
REG_ADDR     = 0x09
REG_CHANNR   = 0x0A
REG_FSCTRL1  = 0x0B
REG_FSCTRL0  = 0x0C
REG_FREQ2    = 0x0D
REG_FREQ1    = 0x0E
REG_FREQ0    = 0x0F
REG_MDMCFG4  = 0x10
REG_MDMCFG3  = 0x11
REG_MDMCFG2  = 0x12
REG_MDMCFG1  = 0x13
REG_MDMCFG0  = 0x14
REG_DEVIATN  = 0x15
REG_MCSM2    = 0x16
REG_MCSM1    = 0x17
REG_MCSM0    = 0x18
REG_FOCCFG   = 0x19
REG_BSCFG    = 0x1A
REG_AGCCTRL2 = 0x1B
REG_AGCCTRL1 = 0x1C
REG_AGCCTRL0 = 0x1D
REG_WOREVT1  = 0x1E
REG_WOREVT0  = 0x1F
REG_WORCTRL  = 0x20
REG_FREND1   = 0x21
REG_FREND0   = 0x22
REG_FSCAL3   = 0x23
REG_FSCAL2   = 0x24
REG_FSCAL1   = 0x25
REG_FSCAL0   = 0x26
REG_TEST2    = 0x2C
REG_TEST1    = 0x2D
REG_TEST0    = 0x2E

# Command Strobes
STROBE_SRES   = 0x30
STROBE_SFSTXON= 0x31
STROBE_SXOFF  = 0x32
STROBE_SCAL   = 0x33
STROBE_SRX    = 0x34
STROBE_STX    = 0x35
STROBE_SIDLE  = 0x36
STROBE_SFRX   = 0x3A
STROBE_SFTX   = 0x3B
STROBE_SNOP   = 0x3D

# Status Registers (read with 0xC0 | addr)
STATUS_PARTNUM   = 0x30
STATUS_VERSION   = 0x31
STATUS_FREQEST   = 0x32
STATUS_MARCSTATE = 0x35
STATUS_TXBYTES   = 0x3A
STATUS_RXBYTES   = 0x3B

# Multi-byte registers
PATABLE = 0x3E
FIFO    = 0x3F

# SPI masks
WRITE_BURST = 0x40
READ_SINGLE = 0x80
READ_BURST  = 0xC0


class CC1101:
    """Controls a CC1101 Sub-1GHz transceiver via SPI on a Raspberry Pi."""

    def __init__(self, bus: int = 0, device: int = 0, speed_hz: int = 4000000):
        self.bus = bus
        self.device = device
        self.speed_hz = speed_hz
        self.spi = None
        self.is_mock = False
        self.verified = None          # True once the chip has proven it answers; None = not checked
        self.problem = None           # plain-English reason when it isn't working

    # ------------------------------------------------------------------
    # SPI Primitives
    # ------------------------------------------------------------------

    def init_spi(self):
        try:
            import spidev
            self.spi = spidev.SpiDev()
            self.spi.open(self.bus, self.device)
            self.spi.max_speed_hz = self.speed_hz
            self.spi.mode = 0b00
            logger.info(f"Initialized SPI bus {self.bus}, device {self.device} at {self.speed_hz} Hz")
        except (ImportError, FileNotFoundError, PermissionError) as e:
            logger.warning(f"Could not open hardware SPI ({e}). Falling back to simulation mode.")
            self.is_mock = True

    def close(self):
        if self.spi and not self.is_mock:
            self.spi.close()

    def _xfer(self, data: List[int]) -> List[int]:
        if self.is_mock:
            return [0] * len(data)
        return self.spi.xfer2(data)

    def write_reg(self, reg: int, val: int):
        self._xfer([reg & 0x3F, val & 0xFF])

    def read_reg(self, reg: int) -> int:
        resp = self._xfer([(reg & 0x3F) | READ_SINGLE, 0x00])
        return resp[1]

    def write_burst(self, start_reg: int, values: List[int]):
        data = [(start_reg & 0x3F) | WRITE_BURST] + [v & 0xFF for v in values]
        self._xfer(data)

    def read_burst(self, start_reg: int, length: int) -> List[int]:
        cmd = [(start_reg & 0x3F) | READ_BURST] + [0x00] * length
        return self._xfer(cmd)[1:]

    def read_status(self, reg: int) -> int:
        """Status registers can read wrong while they're changing (CC1101 errata, SPI read
        synchronization): read until two consecutive reads agree."""
        last = self._xfer([(reg & 0x3F) | READ_BURST, 0x00])[1]
        for _ in range(5):
            now = self._xfer([(reg & 0x3F) | READ_BURST, 0x00])[1]
            if now == last:
                return now
            last = now
        return last

    def strobe(self, cmd: int) -> int:
        resp = self._xfer([cmd & 0x3F])
        return resp[0]

    def reset(self):
        if self.is_mock:
            return
        self.strobe(STROBE_SRES)
        time.sleep(0.015)

    def get_version(self) -> int:
        if self.is_mock:
            return 0x14
        return self.read_status(STATUS_VERSION)

    def get_partnum(self) -> int:
        if self.is_mock:
            return 0x00
        return self.read_status(STATUS_PARTNUM)

    def get_state(self) -> int:
        if self.is_mock:
            return 0x01
        return self.read_status(STATUS_MARCSTATE) & 0x1F

    # ------------------------------------------------------------------
    # Mode A: FIFO Packet Mode (no extra wires needed)
    # ------------------------------------------------------------------

    def setup_security_plus_315mhz(self, packet_len: int = 50):
        """
        Configure CC1101 for Security+ 1.0 OOK FIFO mode.

        Key settings:
          MDMCFG2 = 0x30  → ASK/OOK, SYNC_MODE=000 (no preamble/sync added by HW)
          MDMCFG1 = 0x00  → ignored when SYNC_MODE=000; no preamble inserted
          PKTCTRL0 = 0x00 → fixed length, normal FIFO, no whitening, no CRC
          FREND0   = 0x11 → PATABLE index 1 used for OOK '1' bit
          PATABLE  = [0x00, 0xC0] → '0'=carrier off, '1'=+10 dBm
        """
        self.reset()
        self.strobe(STROBE_SIDLE)

        config = {
            REG_IOCFG2:   0x29,  # GDO2: Chip RDYn
            REG_IOCFG0:   0x06,  # GDO0: sync / packet done assertion
            REG_FIFOTHR:  0x07,  # FIFO threshold (33 bytes)
            REG_PKTLEN:   packet_len & 0xFF,
            REG_PKTCTRL1: 0x00,  # No address check, no status appended
            REG_PKTCTRL0: 0x00,  # Fixed length, FIFO mode, no whitening, no CRC
            REG_FSCTRL1:  0x06,
            REG_FSCTRL0:  0x00,
            # 315.000 MHz with 26 MHz crystal: FREQ = round(315e6/26e6 * 65536) = 0x0C1D89
            REG_FREQ2:    0x0C,
            REG_FREQ1:    0x1D,
            REG_FREQ0:    0x89,
            # Baud rate 2000: DRATE_E=6, DRATE_M=67 → 2002.24 baud (0.11% error)
            REG_MDMCFG4:  0xC6,  # CHANBW=100kHz, DRATE_E=6
            REG_MDMCFG3:  0x43,  # DRATE_M=67
            # ASK/OOK, NO preamble, NO sync word (SYNC_MODE=000 disables both)
            REG_MDMCFG2:  0x30,
            REG_MDMCFG1:  0x00,  # No preamble (ignored when SYNC_MODE=000)
            REG_MDMCFG0:  0x00,
            REG_DEVIATN:  0x15,
            REG_MCSM1:    0x30,  # After TX: go to IDLE; after RX: go to IDLE
            REG_MCSM0:    0x18,  # Calibrate on IDLE→TX/RX
            REG_FOCCFG:   0x16,
            REG_BSCFG:    0x6C,
            REG_AGCCTRL2: 0x03,
            REG_AGCCTRL1: 0x40,
            REG_AGCCTRL0: 0x91,
            REG_FREND1:   0x56,
            REG_FREND0:   0x11,  # OOK: PATABLE index 1 for '1'
            REG_FSCAL3:   0xE9,
            REG_FSCAL2:   0x2A,
            REG_FSCAL1:   0x00,
            REG_FSCAL0:   0x1F,
            REG_TEST2:    0x81,
            REG_TEST1:    0x35,
            REG_TEST0:    0x09,
        }

        for reg, val in config.items():
            self.write_reg(reg, val)

        # PATABLE: index 0 = OOK '0' (RF off), index 1 = OOK '1' (+10 dBm)
        self.write_burst(PATABLE, [0x00, 0xC0])
        self.strobe(STROBE_SFTX)
        time.sleep(0.001)
        self.verify(config)

    def verify(self, config: dict = None) -> bool:
        """Prove the chip is really there: right identity, and the settings we wrote read back
        the same. That only works if power, ground, all four SPI wires and the chip are good.
        Sets self.verified / self.problem and returns verified."""
        if self.is_mock:
            self.verified, self.problem = False, "No SPI device: running in simulation"
            return False
        part, ver = self.get_partnum(), self.get_version()
        if ver in (0x00, 0xFF) and part in (0x00, 0xFF):
            self.verified = False
            self.problem = (f"CC1101 not answering (reads 0x{ver:02X}). Check 3.3 V, GND and the SPI wires "
                            f"(MISO, MOSI, SCK, CSN)")
        elif ver not in (0x04, 0x14):
            self.verified = False
            self.problem = f"Unexpected chip (part 0x{part:02X}, version 0x{ver:02X}): is this a CC1101?"
        else:
            bad = []
            for reg, want in (config or {}).items():
                got = self.read_reg(reg)
                if got != want:
                    bad.append(f"0x{reg:02X}: wrote 0x{want:02X}, read 0x{got:02X}")
            if bad:
                self.verified = False
                self.problem = ("CC1101 answers but its settings didn't stick (" + "; ".join(bad[:3]) +
                                "). Usually a loose MOSI or SCK wire, or an unstable 3.3 V supply")
            else:
                self.verified, self.problem = True, None
        if not self.verified:
            logger.error(f"[CC1101] {self.problem}")
        return bool(self.verified)

    def transmit_burst(self, burst_bytes: bytes):
        """
        Load burst into TX FIFO and transmit. Waits until TX complete.
        burst_bytes must be <= 64 bytes.
        """
        if len(burst_bytes) > 64:
            raise ValueError(f"Burst ({len(burst_bytes)} B) exceeds CC1101 64-byte FIFO")

        if self.is_mock:
            time.sleep(len(burst_bytes) * 8 / 2000.0)
            return

        self.strobe(STROBE_SIDLE)
        time.sleep(0.001)
        self.strobe(STROBE_SFTX)

        self.write_burst(FIFO, list(burst_bytes))
        queued = self.read_status(STATUS_TXBYTES) & 0x7F
        if queued != len(burst_bytes):
            self.strobe(STROBE_SFTX)
            raise RadioError(f"The CC1101 didn't take the data ({queued} of {len(burst_bytes)} bytes). "
                             f"Check the MOSI/MISO/SCK/CSN wiring and the 3.3 V supply")

        self.strobe(STROBE_STX)

        # Wait for TX to complete: 50 bytes × 8 bits / 2000 baud ≈ 200 ms
        tx_duration_sec = (len(burst_bytes) * 8) / 2000.0
        time.sleep(tx_duration_sec + 0.020)

        # Poll for IDLE (state 0x01) up to 300 ms
        state = None
        deadline = time.time() + 0.3
        while time.time() < deadline:
            state = self.get_state()
            if state == 0x01:
                break
            time.sleep(0.005)

        left = self.read_status(STATUS_TXBYTES)
        self.strobe(STROBE_SIDLE)
        if left & 0x80 or state == 0x16:
            self.strobe(STROBE_SFTX)
            raise RadioError("The CC1101 ran out of data while sending (TX underflow)")
        if left & 0x7F:
            self.strobe(STROBE_SFTX)
            raise RadioError(f"The CC1101 didn't send the burst ({left & 0x7F} of {len(burst_bytes)} bytes "
                             f"still waiting, radio state 0x{(state or 0):02X}). It may be stuck: try "
                             f"restarting Halo, and check the 3.3 V supply")

    def transmit_security_plus(self, bursts: List[bytes], inter_burst_delay: float = 0.025):
        """Transmit multiple repeated bursts of Security+ frames (FIFO mode)."""
        for i, burst in enumerate(bursts):
            self.transmit_burst(burst)
            if i < len(bursts) - 1 and inter_burst_delay > 0:
                time.sleep(inter_burst_delay)

    # ------------------------------------------------------------------
    # Mode B: Async Transparent Mode via GDO0 + pigpio (recommended)
    # ------------------------------------------------------------------

    def setup_async_ook_315mhz(self):
        """
        Configure CC1101 for asynchronous serial (transparent) OOK mode.

        In this mode:
          - PKTCTRL0 bits[5:4] = 11 → Asynchronous serial mode
          - GDO0 becomes a serial data INPUT from the Pi
          - The CC1101 simply keys the 315 MHz carrier on/off based on GDO0
          - No FIFO, no preamble, no sync word, no framing overhead
          - Pi must drive GDO0 GPIO with hardware-timed pulses (pigpio waveforms)

        Wiring required:
          CC1101 GDO0  →  Pi GPIO24 (BCM)  [any free GPIO works]
        """
        self.reset()
        self.strobe(STROBE_SIDLE)

        config = {
            # GDO0 = serial data input in async TX mode (0x2D = CLK_XOSC/192, doesn't matter)
            REG_IOCFG0:   0x2D,
            REG_FSCTRL1:  0x06,
            REG_FSCTRL0:  0x00,
            REG_FREQ2:    0x0C,
            REG_FREQ1:    0x1D,
            REG_FREQ0:    0x89,
            REG_MDMCFG4:  0xC6,
            REG_MDMCFG3:  0x43,
            REG_MDMCFG2:  0x30,  # ASK/OOK
            REG_MDMCFG1:  0x00,
            REG_MDMCFG0:  0x00,
            # Async serial mode: PKT_FORMAT=11 (bits[5:4]=11), infinite length
            REG_PKTCTRL0: 0x32,
            REG_MCSM1:    0x30,
            REG_MCSM0:    0x18,
            REG_FOCCFG:   0x16,
            REG_BSCFG:    0x6C,
            REG_AGCCTRL2: 0x03,
            REG_AGCCTRL1: 0x40,
            REG_AGCCTRL0: 0x91,
            REG_FREND1:   0x56,
            REG_FREND0:   0x11,
            REG_FSCAL3:   0xE9,
            REG_FSCAL2:   0x2A,
            REG_FSCAL1:   0x00,
            REG_FSCAL0:   0x1F,
            REG_TEST2:    0x81,
            REG_TEST1:    0x35,
            REG_TEST0:    0x09,
        }
        for reg, val in config.items():
            self.write_reg(reg, val)

        self.write_burst(PATABLE, [0x00, 0xC0])
        self.verify(config)

    def transmit_async_bits(self, burst_bits: List[int], gdo0_gpio: int = 24,
                            bit_duration_us: int = 500):
        """
        Transmit a list of OOK bits (0 or 1) via CC1101 async mode using
        pigpio DMA waveforms for microsecond-accurate timing.

        Parameters:
            burst_bits:      List of 0/1 values (from security_plus.symbols_to_bits)
            gdo0_gpio:       BCM GPIO pin connected to CC1101 GDO0 (default: 24)
            bit_duration_us: Duration per bit in µs (default: 500 for 2000 baud)

        Requires: pigpio daemon running (sudo pigpiod)
                  CC1101 GDO0 connected to gdo0_gpio
        """
        if self.is_mock:
            logger.info(f"[MOCK] Would transmit {len(burst_bits)} bits async on GPIO{gdo0_gpio}")
            time.sleep(len(burst_bits) * bit_duration_us / 1_000_000)
            return

        try:
            import pigpio
        except ImportError:
            raise RuntimeError(
                "pigpio not available. Install with: sudo apt install python3-pigpio && sudo pigpiod"
            )

        pi = pigpio.pi()
        if not pi.connected:
            raise RuntimeError(
                "Cannot connect to pigpio daemon. Run: sudo pigpiod"
            )

        try:
            pi.set_mode(gdo0_gpio, pigpio.OUTPUT)
            pi.write(gdo0_gpio, 0)

            # Build pigpio waveform from run-length encoded bit transitions
            # Compress consecutive identical bits into single waveform pulses
            wave_pulses = []
            i = 0
            while i < len(burst_bits):
                bit = burst_bits[i]
                # Count consecutive same bits
                run = 1
                while i + run < len(burst_bits) and burst_bits[i + run] == bit:
                    run += 1
                duration_us = run * bit_duration_us
                if bit == 1:
                    pulse = pigpio.pulse(1 << gdo0_gpio, 0, duration_us)
                else:
                    pulse = pigpio.pulse(0, 1 << gdo0_gpio, duration_us)
                wave_pulses.append(pulse)
                i += run

            # Ensure GDO0 ends LOW
            wave_pulses.append(pigpio.pulse(0, 1 << gdo0_gpio, bit_duration_us))

            # Put CC1101 into TX (async mode — carrier follows GDO0)
            self.strobe(STROBE_SIDLE)
            time.sleep(0.001)
            self.strobe(STROBE_STX)
            time.sleep(0.002)  # Allow PLL to lock

            # Upload and fire waveform
            pi.wave_clear()
            pi.wave_add_generic(wave_pulses)
            wave_id = pi.wave_create()

            if wave_id < 0:
                raise RuntimeError(f"pigpio wave_create failed: {wave_id}")

            pi.wave_send_once(wave_id)

            # Wait for waveform transmission to complete
            tx_duration_sec = len(burst_bits) * bit_duration_us / 1_000_000
            deadline = time.time() + tx_duration_sec + 0.05
            while time.time() < deadline:
                if pi.wave_tx_busy() == 0:
                    break
                time.sleep(0.005)

            pi.wave_delete(wave_id)

        finally:
            pi.write(gdo0_gpio, 0)
            self.strobe(STROBE_SIDLE)
            pi.stop()

    def transmit_security_plus_async(self, all_burst_bits: List[List[int]],
                                     gdo0_gpio: int = 24,
                                     bit_duration_us: int = 500,
                                     inter_burst_delay: float = 0.025):
        """
        Transmit repeated Security+ bursts using pigpio async mode.

        Parameters:
            all_burst_bits:    List of bit lists (one per repeat)
            gdo0_gpio:         BCM GPIO connected to CC1101 GDO0
            bit_duration_us:   500 µs per bit (2000 baud)
            inter_burst_delay: Gap between repeats in seconds (default 25 ms)
        """
        for i, burst_bits in enumerate(all_burst_bits):
            self.transmit_async_bits(burst_bits, gdo0_gpio=gdo0_gpio,
                                     bit_duration_us=bit_duration_us)
            if i < len(all_burst_bits) - 1 and inter_burst_delay > 0:
                time.sleep(inter_burst_delay)
