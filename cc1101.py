"""
TI CC1101 SPI Transceiver Driver for Raspberry Pi.
Configured for 315.00 MHz ASK/OOK Transmission.
"""

import time
import logging
from typing import List, Union

logger = logging.getLogger(__name__)

# CC1101 Configuration Registers
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
STROBE_SRES   = 0x30  # Reset chip
STROBE_SFSTXON= 0x31  # Enable and calibrate freq synth
STROBE_SXOFF  = 0x32  # Turn off crystal oscillator
STROBE_SCAL   = 0x33  # Calibrate freq synth and turn off
STROBE_SRX    = 0x34  # Enable RX
STROBE_STX    = 0x35  # Enable TX
STROBE_SIDLE  = 0x36  # Exit RX / TX, turn off freq synth
STROBE_SFRX   = 0x3A  # Flush RX FIFO
STROBE_SFTX   = 0x3B  # Flush TX FIFO
STROBE_SNOP   = 0x3D  # No operation (returns status)

# Status Registers (read with burst bit set: 0xC0 | addr)
STATUS_PARTNUM   = 0x30
STATUS_VERSION   = 0x31
STATUS_FREQEST   = 0x32
STATUS_MARCSTATE = 0x35
STATUS_TXBYTES   = 0x3A
STATUS_RXBYTES   = 0x3B

# Multi-byte registers
PATABLE = 0x3E
FIFO    = 0x3F

# SPI read/write masks
WRITE_BURST = 0x40
READ_SINGLE = 0x80
READ_BURST  = 0xC0


class CC1101:
    """Controls a CC1101 Sub-1GHz transceiver via SPI on a Raspberry Pi."""

    def __init__(self, bus: int = 0, device: int = 0, speed_hz: int = 5000000):
        self.bus = bus
        self.device = device
        self.speed_hz = speed_hz
        self.spi = None
        self.is_mock = False

    def init_spi(self):
        """Initialize the SPI bus."""
        try:
            import spidev
            self.spi = spidev.SpiDev()
            self.spi.open(self.bus, self.device)
            self.spi.max_speed_hz = self.speed_hz
            self.spi.mode = 0b00  # SPI Mode 0
            logger.info(f"Initialized SPI bus {self.bus}, device {self.device} at {self.speed_hz} Hz")
        except (ImportError, FileNotFoundError, PermissionError) as e:
            logger.warning(f"Could not open hardware SPI ({e}). Falling back to simulation mode.")
            self.is_mock = True

    def close(self):
        """Close the SPI device."""
        if self.spi and not self.is_mock:
            self.spi.close()

    def _xfer(self, data: List[int]) -> List[int]:
        if self.is_mock:
            # Simulated response
            return [0] * len(data)
        return self.spi.xfer2(data)

    def write_reg(self, reg: int, val: int):
        """Write a single 8-bit register."""
        self._xfer([reg & 0x3F, val & 0xFF])

    def read_reg(self, reg: int) -> int:
        """Read a single 8-bit register."""
        resp = self._xfer([(reg & 0x3F) | READ_SINGLE, 0x00])
        return resp[1]

    def write_burst(self, start_reg: int, values: List[int]):
        """Write consecutive registers or FIFO burst."""
        data = [(start_reg & 0x3F) | WRITE_BURST] + [v & 0xFF for v in values]
        self._xfer(data)

    def read_burst(self, start_reg: int, length: int) -> List[int]:
        """Read consecutive registers."""
        cmd = [(start_reg & 0x3F) | READ_BURST] + [0x00] * length
        resp = self._xfer(cmd)
        return resp[1:]

    def read_status(self, reg: int) -> int:
        """Read a status register."""
        resp = self._xfer([(reg & 0x3F) | READ_BURST, 0x00])
        return resp[1]

    def strobe(self, cmd: int) -> int:
        """Send a command strobe and return the chip status byte."""
        resp = self._xfer([cmd & 0x3F])
        return resp[0]

    def reset(self):
        """Reset the CC1101 to default settings."""
        if self.is_mock:
            return
        self.strobe(STROBE_SRES)
        time.sleep(0.01)

    def get_version(self) -> int:
        """Read the chip VERSION register (typically 0x14 or 0x04 for CC1101)."""
        if self.is_mock:
            return 0x14
        return self.read_status(STATUS_VERSION)

    def get_partnum(self) -> int:
        """Read the chip PARTNUM register (typically 0x00 for CC1101)."""
        if self.is_mock:
            return 0x00
        return self.read_status(STATUS_PARTNUM)

    def get_state(self) -> int:
        """Read current MARCSTATE (0x01=IDLE, 0x13..0x15=TX, etc.)."""
        if self.is_mock:
            return 0x01
        return self.read_status(STATUS_MARCSTATE) & 0x1F

    def setup_security_plus_315mhz(self, packet_len: int = 50):
        """
        Configure the CC1101 for Security+ 1.0 (Purple learn button):
        - Frequency: 315.00 MHz
        - Modulation: ASK / OOK
        - Baud rate: 2000 baud (500 µs per bit)
        - TX Power: +10 dBm
        """
        self.reset()
        self.strobe(STROBE_SIDLE)

        # Configuration registers for 315 MHz ASK/OOK at 2000 baud
        config = {
            REG_IOCFG2:   0x29,  # Chip RDYn
            REG_IOCFG0:   0x06,  # Asserts when sync or packet processed
            REG_FIFOTHR:  0x07,  # FIFO threshold
            REG_PKTLEN:   packet_len & 0xFF,  # Fixed packet length
            REG_PKTCTRL1: 0x00,  # No address check
            REG_PKTCTRL0: 0x00,  # Fixed length, CRC disabled, no whitening
            REG_FSCTRL1:  0x06,  # Frequency synthesizer IF
            REG_FSCTRL0:  0x00,
            # 315.00 MHz Carrier Frequency (for 26 MHz crystal: 0x0C1D89)
            REG_FREQ2:    0x0C,
            REG_FREQ1:    0x1D,
            REG_FREQ0:    0x89,
            # Modem: DRATE_E=6 (MDMCFG4 lower nibble), CHANBW=100kHz
            REG_MDMCFG4:  0xC6,
            # Modem: DRATE_M=67 (0x43) -> 2002.24 bps (0.1% off from 2000 bps)
            REG_MDMCFG3:  0x43,
            # Modem: ASK/OOK modulation (0x30 = ASK/OOK, no sync word)
            REG_MDMCFG2:  0x30,
            REG_MDMCFG1:  0x00,  # No preamble
            REG_MDMCFG0:  0x00,
            REG_DEVIATN:  0x15,
            REG_MCSM1:    0x30,  # Return to IDLE after TX
            REG_MCSM0:    0x18,  # Auto calibrate on IDLE to TX
            REG_FOCCFG:   0x16,
            REG_BSCFG:    0x6C,
            REG_AGCCTRL2: 0x03,
            REG_AGCCTRL1: 0x40,
            REG_AGCCTRL0: 0x91,
            REG_FREND1:   0x56,
            # ASK/OOK mode: select PATABLE index 1 for '1', index 0 for '0'
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

        # PA Table: index 0 = 0x00 (OOK Off), index 1 = 0xC0 (+10 dBm OOK On)
        self.write_burst(PATABLE, [0x00, 0xC0])

        # Flush TX FIFO
        self.strobe(STROBE_SFTX)

    def transmit_burst(self, burst_bytes: bytes):
        """
        Load a burst of bytes into the TX FIFO and transmit it.
        Waits until transmission is complete.
        """
        if len(burst_bytes) > 64:
            raise ValueError(f"Burst size ({len(burst_bytes)}) exceeds CC1101 FIFO size (64 bytes)")

        if self.is_mock:
            time.sleep(len(burst_bytes) * 8 / 2000.0)
            return

        # Ensure IDLE state and flush TX FIFO
        self.strobe(STROBE_SIDLE)
        self.strobe(STROBE_SFTX)

        # Write data to TX FIFO
        self.write_burst(FIFO, list(burst_bytes))

        # Enter TX mode
        self.strobe(STROBE_STX)

        # Calculate transmission duration at 2000 baud
        tx_duration_sec = (len(burst_bytes) * 8) / 2000.0

        # Wait for transmission to complete
        time.sleep(tx_duration_sec + 0.015)

        # Verify radio returned to IDLE
        start = time.time()
        while (self.get_state() != 0x01) and (time.time() - start < 0.2):
            time.sleep(0.005)

        self.strobe(STROBE_SIDLE)

    def transmit_security_plus(self, bursts: List[bytes], inter_burst_delay: float = 0.02):
        """Transmit multiple repeated bursts of Security+ frames."""
        for i, burst in enumerate(bursts):
            self.transmit_burst(burst)
            if i < len(bursts) - 1 and inter_burst_delay > 0:
                time.sleep(inter_burst_delay)
