"""
Chamberlain / LiftMaster Security+ 1.0 (Purple Learn Button, 315 MHz)
Protocol Encoder and Frame Generator.

Security+ 1.0 protocol characteristics:
- Carrier Frequency: 315.00 MHz
- Modulation: ASK / OOK (Amplitude Shift Keying / On-Off Keying)
- Symbol Duration: 2.0 ms (2000 µs)
- Ternary Alphabet:
    Symbol 0: 1500 µs LOW, 500 µs HIGH  (Bit pattern at 2000 baud: 0 0 0 1)
    Symbol 1: 1000 µs LOW, 1000 µs HIGH (Bit pattern at 2000 baud: 0 0 1 1)
    Symbol 2: 500 µs LOW, 1500 µs HIGH  (Bit pattern at 2000 baud: 0 1 1 1)
- Packet Structure:
    Each transmission consists of two packets separated by a ~58 ms blank pause:
    - Packet 1: Sync symbol 0 + 20 payload symbols (first half of 40-symbol code)
    - Inter-packet Pause: ~58 ms (116 bits of LOW at 2000 baud)
    - Packet 2: Sync symbol 2 + 20 payload symbols (second half of 40-symbol code)
    - Trailing Pause: ~58 ms
- Fixed code: 20-trit integer (< 3^20 = 3,486,784,401) representing the remote ID and button.
- Rolling code: 32-bit integer (< 2^32) incremented with every button press.
"""

from typing import List, Tuple


def reverse_int(n: int, width: int) -> int:
    """Reverse the binary representation of an integer of given bit width."""
    b = "{:0{width}b}".format(n, width=width)
    return int(b[::-1], 2)


def encode(rolling: int, fixed: int) -> List[int]:
    """
    Encode a Security+ 1.0 rolling and fixed code into 40 ternary payload symbols (0, 1, 2).

    :param rolling: 32-bit integer rolling counter (0 to 2^32 - 1)
    :param fixed: 20-trit integer fixed remote ID (0 to 3^20 - 1)
    :return: List of 40 integers in {0, 1, 2}
    """
    if rolling >= 2**32:
        raise ValueError("Rolling code must be less than 2^32")
    if fixed >= 3**20:
        raise ValueError("Fixed code must be less than 3^20")

    # Security+ 1.0 masks LSB and reverses 32 bits
    rev_rolling = reverse_int(rolling & 0xFFFFFFFE, 32)

    rolling_base3 = [0] * 20
    fixed_base3 = [0] * 20

    for i in range(19, -1, -1):
        rolling_base3[i] = rev_rolling % 3
        rev_rolling //= 3
        fixed_base3[i] = fixed % 3
        fixed //= 3

    code = []
    acc = 0
    for i in range(20):
        if i in (0, 10):
            acc = 0
        acc += rolling_base3[i]
        code.append(rolling_base3[i])
        acc += fixed_base3[i]
        code.append(acc % 3)

    return code


def decode(code: List[int]) -> Tuple[int, int]:
    """
    Decode 40 ternary symbols back into (rolling_code, fixed_code).

    :param code: List of 40 integers in {0, 1, 2}
    :return: (rolling_code, fixed_code)
    """
    if len(code) != 40:
        raise ValueError("Code must contain exactly 40 symbols")

    rolling_base3 = [0] * 20
    fixed_base3 = [0] * 20

    for i in range(20):
        if i in (0, 10):
            acc = 0
        r = code[2 * i]
        rolling_base3[i] = r
        acc += r
        f = (code[2 * i + 1] - acc) % 3
        fixed_base3[i] = f
        acc += f

    rolling = 0
    fixed = 0
    for r in rolling_base3:
        rolling = rolling * 3 + r
    for f in fixed_base3:
        fixed = fixed * 3 + f

    rolling = reverse_int(rolling, 32)
    return rolling, fixed


def symbols_to_bits(symbols: List[int]) -> List[int]:
    """
    Convert ternary symbols to 2000 baud OOK bits (500 µs per bit).
    Security+ 1.0 official spec (argilo/secplus):
      Symbol 0: 1500µs OFF, 500µs ON  -> [0, 0, 0, 1]
      Symbol 1: 1000µs OFF, 1000µs ON -> [0, 0, 1, 1]
      Symbol 2: 500µs OFF, 1500µs ON  -> [0, 1, 1, 1]
    """
    lut = {
        0: [0, 0, 0, 1],
        1: [0, 0, 1, 1],
        2: [0, 1, 1, 1],
    }
    bits = []
    for s in symbols:
        if s not in lut:
            raise ValueError(f"Invalid symbol: {s}")
        bits.extend(lut[s])
    return bits


def bits_to_bytes(bits: List[int]) -> bytes:
    """Pack a list of binary bits (0 or 1, MSB first) into bytes."""
    out = bytearray()
    byte = 0
    bit_count = 0
    for b in bits:
        byte = (byte << 1) | (1 if b else 0)
        bit_count += 1
        if bit_count == 8:
            out.append(byte)
            byte = 0
            bit_count = 0
    if bit_count > 0:
        byte <<= (8 - bit_count)
        out.append(byte)
    return bytes(out)


def generate_burst_bits(rolling: int, fixed: int, pause_ms: float = 58.0) -> List[int]:
    """
    Generate one full Security+ 1.0 transmission burst as a flat list of OOK bits.

    Returns the raw list of 0/1 bits (400 total = 50 bytes at 2000 baud).
    Use this with CC1101 async/pigpio mode for microsecond-accurate transmission.

    Packet 1: Sync 0 + symbols[0..19] (21 symbols = 84 bits)
    Pause 1:  58 ms of LOW (116 bits at 2000 baud)
    Packet 2: Sync 2 + symbols[20..39] (21 symbols = 84 bits)
    Pause 2:  58 ms of LOW (116 bits at 2000 baud)
    Total: 400 bits
    """
    code = encode(rolling, fixed)

    p1_symbols = [0] + code[:20]
    p2_symbols = [2] + code[20:]

    p1_bits = symbols_to_bits(p1_symbols)
    p2_bits = symbols_to_bits(p2_symbols)

    pause_bits_count = int(round(pause_ms / 0.5))
    pause_bits = [0] * pause_bits_count

    return p1_bits + pause_bits + p2_bits + pause_bits


def generate_burst_bytes(rolling: int, fixed: int, pause_ms: float = 58.0) -> bytes:
    """
    Generate one full Security+ transmission burst (Packet 1 + Pause + Packet 2 + Pause)
    formatted as raw 2000 baud OOK bytes.

    Packet 1: Sync 0 + symbols[0..19] (21 symbols = 84 bits)
    Pause 1: 58 ms pause (116 bits)
    Packet 2: Sync 2 + symbols[20..39] (21 symbols = 84 bits)
    Pause 2: 58 ms pause (116 bits)
    Total: exactly 400 bits = 50 bytes.
    """
    code = encode(rolling, fixed)

    p1_symbols = [0] + code[:20]
    p2_symbols = [2] + code[20:]

    p1_bits = symbols_to_bits(p1_symbols)
    p2_bits = symbols_to_bits(p2_symbols)

    # 1 bit = 0.5 ms at 2000 baud
    pause_bits_count = int(round(pause_ms / 0.5))
    pause_bits = [0] * pause_bits_count

    burst_bits = p1_bits + pause_bits + p2_bits + pause_bits
    return bits_to_bytes(burst_bits)
